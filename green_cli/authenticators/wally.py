import wallycore as wally

from green_cli.authenticators import *

import secrets

class WallyAuthenticator(MnemonicOnDisk, HardwareDevice):
    """Stores mnemonic on disk but does not pass it to the gdk

    This class illustrates how the hardware device interface to the gdk can be used to implement all
    required crypto operations external to the gdk and thus avoid passing any key material to the
    gdk at all.
    """

    @property
    def name(self):
        return 'libwally software signer'

    # By default prefer to use Anti-Exfil signatures
    # (sacrifices low-r guarantee)
    @property
    def default_hw_device_info(self):
        return {'device': {
                  'name': self.name,
                  'supports_low_r': False,
                  'supports_liquid': 0,
                  'supports_host_unblinding': False,
                  'supports_ae_protocol': 1,
                  'supports_arbitrary_scripts': True}
               }

    def create(self, session_obj, words):
        """Create and register a new wallet"""
        entropy_len = int(words * 4 / 3);
        entropy = secrets.token_bytes(entropy_len)

        wordlist = wally.bip39_get_wordlist('en')
        mnemonic = wally.bip39_mnemonic_from_bytes(wordlist, entropy)
        assert len(mnemonic.split()) == words

        self._mnemonic = mnemonic
        return self.register(session_obj)

    @property
    def seed(self):
        _, seed = wally.bip39_mnemonic_to_seed512(self._mnemonic, None)
        return seed

    @property
    def master_key(self):
        return wally.bip32_key_from_seed(self.seed, wally.BIP32_VER_TEST_PRIVATE,
                                         wally.BIP32_FLAG_KEY_PRIVATE)

    def derive_key(self, path: List[int]):
        if not path:
            return self.master_key
        else:
            return wally.bip32_key_from_parent_path(self.master_key, path,
                                                    wally.BIP32_FLAG_KEY_PRIVATE)

    def get_xpub(self, path: List[int]):
        return wally.bip32_key_to_base58(self.derive_key(path), wally.BIP32_FLAG_KEY_PUBLIC)

    def get_privkey(self, path: List[int]) -> bytearray:
        return wally.bip32_key_get_priv_key(self.derive_key(path))

    def _make_ae_signature(self, privkey, signing_hash, details):
        # NOTE: with actual hw these two steps would be separate calls to the hww, as the host does
        #       not reveal 'host_entropy' to the signer until it has received the 'signer_commitment'.

        # 1. Provide host_commitment, receive signer_commitment
        host_commitment = bytes.fromhex(details['ae_host_commitment'])
        signer_commitment = wally.ae_signer_commit_from_bytes(privkey, signing_hash, host_commitment, wally.EC_FLAG_ECDSA)

        # 2. Provide host_entropy, receive signature
        host_entropy = bytes.fromhex(details['ae_host_entropy'])
        signature = wally.ae_sig_from_bytes(privkey, signing_hash, host_entropy, wally.EC_FLAG_ECDSA)

        return signer_commitment, signature

    def sign_message(self, details: Dict) -> Dict:
        message = details['message'].encode('utf-8')
        formatted = wally.format_bitcoin_message(message, wally.BITCOIN_MESSAGE_FLAG_HASH)
        privkey = self.get_privkey(details['path'])

        result = {}
        if details['use_ae_protocol']:
            signer_commitment, signature = self._make_ae_signature(privkey, formatted, details)
            signer_commitment = signer_commitment.hex()
            logging.debug('Signer commitment: %s', signer_commitment)
            result['signer_commitment'] = signer_commitment
        else:
            signature = wally.ec_sig_from_bytes(privkey, formatted,
                                                wally.EC_FLAG_ECDSA | wally.EC_FLAG_GRIND_R)

        result['signature'] = wally.ec_sig_to_der(signature).hex()
        return result

    def _get_sighash(self, wally_tx, index, utxo):
        flags = wally.WALLY_TX_FLAG_USE_WITNESS
        prevout_script = wally.hex_to_bytes(utxo['prevout_script'])
        return wally.tx_get_btc_signature_hash(
                wally_tx, index, prevout_script, utxo['satoshi'], wally.WALLY_SIGHASH_ALL, flags)

    def _sign_tx(self, details, wally_tx):
        txdetails = details['transaction']
        utxos = details['signing_inputs']
        use_ae_protocol = details['use_ae_protocol']

        signatures = []
        signer_commitments = []
        for index, utxo in enumerate(utxos):
            is_segwit = utxo['script_type'] in [14, 15, 159, 162] # FIXME!!
            if not is_segwit:
                # FIXME
                raise NotImplementedError("Non-segwit input")

            txhash = self._get_sighash(wally_tx, index, utxo)
            path = utxo['user_path']
            privkey = self.get_privkey(path)
            logging.debug('Processing input %s, path %s', index, path)

            if use_ae_protocol:
                signer_commitment, signature = self._make_ae_signature(privkey, txhash, utxo)

                signer_commitment = signer_commitment.hex()
                logging.debug('Signer commitment: %s', signer_commitment)
                signer_commitments.append(signer_commitment)
            else:
                signature = wally.ec_sig_from_bytes(privkey, txhash,
                                                    wally.EC_FLAG_ECDSA | wally.EC_FLAG_GRIND_R)

            signature = wally.ec_sig_to_der(signature)
            signature.append(wally.WALLY_SIGHASH_ALL)
            signature = signature.hex()
            logging.debug('Signature (der): %s', signature)
            signatures.append(signature)

        result = {'signer_commitments': signer_commitments} if use_ae_protocol else {}
        result['signatures'] = signatures
        return result

    def sign_tx(self, details: Dict):
        tx_flags = wally.WALLY_TX_FLAG_USE_WITNESS
        wally_tx = wally.tx_from_hex(details['transaction']['transaction'], tx_flags)
        return json.dumps(self._sign_tx(details, wally_tx))


class WallyAuthenticatorLiquid(WallyAuthenticator):

    # By default prefer to use Anti-Exfil signatures
    # (sacrifices low-r guarantee)
    @property
    def default_hw_device_info(self):
        return {'device': {
                  'name': self.name,
                  'supports_low_r': False,
                  'supports_liquid': 1,
                  'supports_host_unblinding': True,
                  'supports_ae_protocol': 1,
                  'supports_arbitrary_scripts': True}
               }

    @property
    def master_blinding_key(self) -> bytes:
        return wally.asset_blinding_key_from_seed(self.seed)

    def get_private_blinding_key(self, script: bytes) -> bytes:
        return wally.asset_blinding_key_to_ec_private_key(self.master_blinding_key, script)

    def get_public_blinding_key(self, script: bytes) -> bytes:
        private_key = self.get_private_blinding_key(script)
        return wally.ec_public_key_from_private_key(private_key)

    def get_shared_nonce(self, pubkey: bytes, script: bytes) -> bytes:
        our_privkey = self.get_private_blinding_key(script)
        nonce = wally.sha256(wally.ecdh(pubkey, our_privkey))
        return nonce

    def _get_blinding_factors(self, txdetails, wally_tx):
        utxos = txdetails['used_utxos'] or txdetails['old_used_utxos']

        for i, o in enumerate(txdetails['transaction_outputs']):
            o['wally_index'] = i

        blinded_outputs = [o for o in txdetails['transaction_outputs'] if not o['is_fee']]
        for output in blinded_outputs:
            # TODO: the derivation dance
            # the following values are in display order, reverse them when converting to bytes
            output['assetblinder'] = os.urandom(32).hex()
            output['amountblinder'] = os.urandom(32).hex()

        endpoints = utxos + blinded_outputs
        values = [endpoint['satoshi'] for endpoint in endpoints]
        abfs = b''.join(bytes.fromhex(endpoint['assetblinder'])[::-1] for endpoint in endpoints)
        vbfs = b''.join(bytes.fromhex(endpoint['amountblinder'])[::-1] for endpoint in endpoints[:-1])
        final_vbf = wally.asset_final_vbf(values, len(utxos), abfs, vbfs)
        blinded_outputs[-1]['amountblinder'] = final_vbf[::-1].hex()

        for o in blinded_outputs:
            asset_commitment = wally.asset_generator_from_bytes(bytes.fromhex(o['asset_id'])[::-1], bytes.fromhex(o['assetblinder'])[::-1])
            value_commitment = wally.asset_value_commitment(o['satoshi'], bytes.fromhex(o['amountblinder'])[::-1], asset_commitment)

            o['asset_commitment'] = asset_commitment.hex()
            o['value_commitment'] = value_commitment.hex()

            # Write the commitments into the wally tx for signing
            wally.tx_set_output_asset(wally_tx, o['wally_index'], asset_commitment)
            wally.tx_set_output_value(wally_tx, o['wally_index'], value_commitment)

        retval = {}
        for key in ['assetblinders', 'amountblinders', 'asset_commitments', 'value_commitments']:
            # gdk expects to get an empty entry for the fee output too, hence this is over the
            # transaction outputs, not just the blinded outputs (fee will just have empty
            # strings)
            retval[key] = [o.get(key[:-1], '') for o in txdetails['transaction_outputs']]
        return retval

    def _get_sighash(self, wally_tx, index, utxo):
        flags = wally.WALLY_TX_FLAG_USE_WITNESS
        prevout_script = wally.hex_to_bytes(utxo['prevout_script'])
        if utxo['confidential']:
            value = bytes.fromhex(utxo['commitment'])
        else:
            value = wally.tx_confidential_value_from_satoshi(utxo['satoshi'])
        return wally.tx_get_elements_signature_hash(
            wally_tx, index, prevout_script, value, wally.WALLY_SIGHASH_ALL, flags)

    def sign_tx(self, details):
        tx_flags = wally.WALLY_TX_FLAG_USE_WITNESS | wally.WALLY_TX_FLAG_USE_ELEMENTS
        wally_tx = wally.tx_from_hex(details['transaction']['transaction'], tx_flags)

        retval = {}
        retval.update(self._get_blinding_factors(details['transaction'], wally_tx))
        retval.update(self._sign_tx(details, wally_tx))

        return json.dumps(retval)


def get_authenticator(options):
    if 'liquid' in options['network']:
        return WallyAuthenticatorLiquid(options)
    else:
        return WallyAuthenticator(options)
