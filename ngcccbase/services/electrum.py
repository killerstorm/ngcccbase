#!/usr/bin/env python

"""
electrum.py

This is a connector to Stratum protocol Electrum servers
For now, the main usage of this file is to grab the utxo's for a
given address.
UTXO's (Unspent Transaction Outputs) are the record of transactions
for an address that haven't been spent yet.
"""

from bitcoin.core import CTransaction
from bitcoin.wallet import CBitcoinAddress
from bitcoin.core import x as to_binary
from bitcoin.core import b2lx as to_little_endian_hex
from bitcoin.core import b2x as to_hex
from coloredcoinlib import blockchain

import json
import Queue
import socket
import sys
import threading
import time
import traceback
import urllib2
import bitcoin.rpc


class ConnectionError(Exception):
    pass


class ElectrumInterface(object):
    """Interface for interacting with Electrum servers using the
    stratum tcp protocol
    """

    def __init__(self, host, port, debug=False):
        """Make an interface object for connecting to electrum server
        """
        self.message_counter = 0
        self.connection = (host, port)
        self.debug = debug
        self.is_connected = False
        self.q = Queue.Queue()
        self.pending_responses = {}
        self.connect()
        self.t = threading.Thread(target=self.grab_responses)
        self.t.daemon = True
        self.t.start()

    def connect(self):
        """Connects to an electrum server via TCP.
        Uses a socket so we can listen for a response
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        try:
            sock.connect(self.connection)
        except:
            raise ConnectionError("Unable to connect to %s:%s" % self.connection)

        sock.settimeout(60)
        self.sock = sock
        self.is_connected = True
        if self.debug:
            print ("connected to %s:%s" % self.connection ) # pragma: no cover
        return True

    def grab_responses(self):
        """Get a response message from an electrum server and push onto queue
        """
        try:
            out = ''
            while self.is_connected or self.connect():
                try:
                    msg = self.sock.recv(1024)
                except socket.timeout:         # pragma: no cover
                    print ("socket timed out") # pragma: no cover
                    self.is_connected = False  # pragma: no cover
                    continue                   # pragma: no cover
                except socket.error:                      # pragma: no cover
                    traceback.print_exc(file=sys.stdout)  # pragma: no cover
                    raise                                 # pragma: no cover

                out += msg
                if msg == '':
                    self.is_connected = False  # pragma: no cover

                # get the list of messages by splitting on newline
                raw_messages = out.split("\n")

                # the last one isn't complete
                out = raw_messages.pop()
                for raw_message in raw_messages:
                    try:
                        message = json.loads(raw_message)
                    except ValueError:
                        continue

                    id = message.get('id')
                    error = message.get('error')
                    result = message.get('result')

                    if error:
                        raise Exception(                    # pragma: no cover
                            "received error %s" % message)  # pragma: no cover
                    else:
                        if self.debug:
                            print "%s: %s" % (id, result)   # pragma: no cover
                        self.q.put([id, result])
        except:                                   # pragma: no cover
            traceback.print_exc(file=sys.stdout)  # pragma: no cover

    def add_request(self, method, params):
        """Send a tx request to electrum and not wait
        """
        current_id = self.message_counter
        self.message_counter += 1
        try:
            self.sock.send(
                json.dumps({
                    'id': current_id,
                    'method': method,
                    'params': params})
                + "\n")
        except socket.error:                       # pragma: no cover
            traceback.print_exc(file=sys.stdout)   # pragma: no cover
            return None                            # pragma: no cover
        return current_id

    def get_async_response(self, target_id, blocking=True):
        if target_id in self.pending_responses:
            return self.pending_responses.pop(target_id)
        try:
            resp_id, raw = self.q.get(blocking)
            while resp_id:
                if resp_id == target_id:
                    return raw
                else:
                    self.pending_responses[resp_id] = raw
                resp_id, raw = self.q.get(blocking)
        except queue.Empty:
            return None

    def get_response(self, method, params):
        target_id = self.add_request(method, params)
        response = self.get_async_response(target_id, True)
        return response

    def get_version(self):
        """Get the server version of the electrum server
        that it's connected to.
        """
        return self.get_response('server.version', ["1.9", "0.6"])

    def get_height(self):
        return self.get_response('blockchain.numblocks.subscribe', [])

    def get_merkle(self, tx_hash, height):
        return self.get_response('blockchain.transaction.get_merkle', [tx_hash,
                                                                       height])

    def get_header(self, height):
        return self.get_response('blockchain.block.get_header', [height])

    def get_raw_transaction(self, tx_id, height):
        """Get the raw transaction that has the transaction hash
        of <tx_id> and height <height>.
        Note you may need to use another method to get the height
        from the transaction id hash.
        """
        return self.get_response('blockchain.transaction.get', [tx_id, height])

    def get_utxo(self, address):
        """Gets all the Unspent Transaction Outs from a given <address>
        """
        script_pubkey = CBitcoinAddress(address).to_scriptPubKey()
        txs = self.get_response('blockchain.address.get_history', [address])
        if not txs:
            return []
        spent = {}
        utxos = []
        # start grabbing responses
        ids = []
        # add the requests in parallel
        for tx in txs:
            id = self.add_request('blockchain.transaction.get',
                                  (tx['tx_hash'], tx['height']))
            ids.append((id,tx))
            
        for id, tx in ids:
            raw = self.get_async_response(id, True)
            if tx:
                data = CTransaction.deserialize(to_binary(raw))
                for vin in data.vin:
                    spent[(to_little_endian_hex(vin.prevout.hash),
                           vin.prevout.n)] = 1
                for outindex, vout in enumerate(data.vout):
                    if vout.scriptPubKey == script_pubkey:
                        utxos += [(tx['tx_hash'], outindex, vout.nValue,
                                   to_hex(vout.scriptPubKey))]
        return [u for u in utxos if not u[0:2] in spent]


class EnhancedBlockchainState(blockchain.BlockchainState):
    """Subclass of coloredcoinlib's BlockchainState for
    getting the raw transaction from electrum instead of a local
    bitcoind server. This is more convenient than reindexing
    the bitcoind server which can take an hour or more.
    """

    def __init__(self, url, port):
        """Initialization takes the url and port of the electrum server.
        We use the normal bitcoind interface, except for
        get_raw_transaction, where we have to do separate lookups
        to blockchain and electrum.
        """
        self.interface = ElectrumInterface(url, port)
        self.bitcoind = bitcoin.rpc.RawProxy()
        self.cur_height = None

    def get_tx_block_height(self, txhash):
        """Get the tx_block_height given a txhash from blockchain.
        This is necessary since the electrum interface requires
        the height in order to get the raw transaction. The parent
        class, BlockchainStatus, uses get_raw_transaction to get
        the height, which would cause a circular dependency.
        """
        url = "https://blockchain.info/rawtx/%s" % txhash
        jsonData = urllib2.urlopen(url).read()
        if jsonData[0] != '{':
            return (None, False)  # pragma: no cover
        data = json.loads(jsonData)
        return (data.get("block_height", None), True)

    def get_raw_transaction(self, txhash):
        try:
            # first, grab the tx height
            height = self.get_tx_block_height(txhash)[0]
            if not height:
                raise Exception("")  # pragma: no cover
            
            # grab the transaction from electrum which
            #  unlike bitcoind requires the height
            raw = self.interface.get_raw_transaction(txhash, height)
        except:                                              # pragma: no cover
            raise Exception(                                 # pragma: no cover
                "Could not connect to blockchain and/or"     # pragma: no cover
                "electrum server to grab the data we need")  # pragma: no cover
        return raw

    def get_tx(self, txhash):
        """Get the transaction object given a transaction hash.
        """
        txhex = self.get_raw_transaction(txhash)
        tx = CTransaction.deserialize(to_binary(txhex))
        return blockchain.CTransaction.from_bitcoincore(txhash, tx, self)

    def get_height(self):
        """Get the current height at the server
        """
        return self.interface.get_height()

    def get_merkle(self, tx_hash):
        height, _ = self.get_tx_block_height(tx_hash)
        return self.interface.get_merkle(tx_hash, height)

    def get_header(self, height):
        return self.interface.get_header(height)
