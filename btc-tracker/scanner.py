"""
scanner.py — BIP84 (Native SegWit / zpub) address derivation and Electrum
server querying.

Talks to the local Fulcrum server on Umbrel over the Electrum TCP protocol
(newline-delimited JSON-RPC, port 50002). Any Electrum-protocol server works,
so Electrs can still be used by pointing ELECTRUM_HOST / ELECTRUM_PORT at it.

For each wallet:
  1. Derive receiving (chain=0) and change (chain=1) addresses using embit.
  2. Compute the Electrum-style scripthash for each address.
  3. Query the server for transaction history per scripthash.
  4. Parse raw transactions with embit to compute net sats per address.
  5. Store addresses and transactions in the database.

Gap limit: stop deriving new addresses once 20 consecutive addresses have no
transactions.
"""

import os
import json
import socket
import struct
import hashlib
import logging

from embit import bip32, script
from embit.networks import NETWORKS
from embit.transaction import Transaction

import db

log = logging.getLogger(__name__)

# Fulcrum on Umbrel: 10.21.21.200:50002 (plain TCP).
# The old ELECTRS_* variable names are still honoured as a fallback.
ELECTRUM_HOST = os.environ.get(
    "ELECTRUM_HOST", os.environ.get("ELECTRS_HOST", "10.21.21.200")
)
ELECTRUM_PORT = int(os.environ.get(
    "ELECTRUM_PORT", os.environ.get("ELECTRS_PORT", "50002")
))

GAP_LIMIT = 20
SOCKET_TIMEOUT = 30  # seconds
CLIENT_NAME = "cold-storage-tracker"
PROTOCOL_VERSION = "1.4"


class ServerUnavailable(Exception):
    """The Electrum server could not be reached or dropped the connection."""


class ElectrumError(Exception):
    """The server answered a request with an error."""


# ---------------------------------------------------------------------------
# Electrum TCP client
# ---------------------------------------------------------------------------

class ElectrumClient:
    """Simple Electrum JSON-RPC client over TCP (newline-delimited JSON)."""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self._sock = None
        self._buffer = b""
        self._id = 0
        self._tx_cache = {}      # txid (hex str) -> raw hex str
        self._header_cache = {}  # block height -> timestamp (int)

    def connect(self):
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=SOCKET_TIMEOUT
            )
        except OSError as exc:
            self._sock = None
            raise ServerUnavailable(
                f"Couldn't connect to Fulcrum at {self.host}:{self.port} ({exc}). "
                "Check that Fulcrum is installed and has finished indexing — "
                "it doesn't accept connections until its initial sync is done."
            ) from exc
        self._buffer = b""
        # Fulcrum expects version negotiation before other calls.
        version = self.call("server.version", [CLIENT_NAME, PROTOCOL_VERSION])
        log.info(
            "Connected to Electrum server at %s:%d (%s)",
            self.host, self.port, version,
        )

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def call(self, method, params=None):
        """Send a JSON-RPC request and return the result."""
        if self._sock is None:
            self.connect()

        self._id += 1
        req = {
            "id": self._id,
            "jsonrpc": "2.0",
            "method": method,
            "params": params or [],
        }
        try:
            self._sock.sendall((json.dumps(req) + "\n").encode())

            # Read until we get a full line (newline-delimited response)
            while b"\n" not in self._buffer:
                chunk = self._sock.recv(65536)
                if not chunk:
                    raise ServerUnavailable(
                        "Fulcrum closed the connection during the scan."
                    )
                self._buffer += chunk
        except OSError as exc:
            raise ServerUnavailable(
                f"Lost connection to Fulcrum at {self.host}:{self.port} ({exc})."
            ) from exc

        line, self._buffer = self._buffer.split(b"\n", 1)
        resp = json.loads(line)

        if "error" in resp and resp["error"]:
            raise ElectrumError(f"Electrum error: {resp['error']}")

        return resp.get("result")

    def get_history(self, scripthash):
        """Get transaction history for a scripthash.
        Returns list of {"tx_hash": str, "height": int}."""
        return self.call("blockchain.scripthash.get_history", [scripthash])

    def get_raw_tx(self, txid):
        """Get raw transaction hex, with caching."""
        if txid in self._tx_cache:
            return self._tx_cache[txid]
        raw = self.call("blockchain.transaction.get", [txid])
        self._tx_cache[txid] = raw
        return raw

    def get_block_time(self, height):
        """Get block timestamp for a given height, with caching."""
        if height in self._header_cache:
            return self._header_cache[height]
        header_hex = self.call("blockchain.block.header", [height])
        # Timestamp is at byte offset 68 in the 80-byte block header
        header_bytes = bytes.fromhex(header_hex)
        timestamp = struct.unpack("<I", header_bytes[68:72])[0]
        self._header_cache[height] = timestamp
        return timestamp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scripthash(addr_script):
    """
    Compute the Electrum-style scripthash from a Script object.
    = SHA256(raw_scriptPubKey), bytes reversed, hex-encoded.
    """
    raw = addr_script.data
    return hashlib.sha256(raw).digest()[::-1].hex()


def _derive_address_and_script(root_key, chain, idx):
    """Derive a BIP84 bc1q... address and its Script (scriptPubKey) object."""
    child = root_key.derive([chain, idx])
    sc = script.p2wpkh(child)
    address = sc.address(NETWORKS["main"])
    return address, sc


def _net_value_for_address(client, raw_hex, addr_script_bytes):
    """
    Compute the net satoshi value of a raw transaction for a specific address.
    Positive = received, negative = sent.

    Parses the raw transaction with embit and compares output/input
    scriptPubKeys against the target address's scriptPubKey bytes.
    """
    tx = Transaction.parse(bytes.fromhex(raw_hex))

    # Sum outputs paying to our address
    received = 0
    for vout in tx.vout:
        if vout.script_pubkey.data == addr_script_bytes:
            received += vout.value

    # Sum inputs spending from our address
    spent = 0
    for vin in tx.vin:
        # Coinbase inputs have txid = 32 zero bytes
        if vin.txid == b'\x00' * 32:
            continue
        # embit stores txid in display order (already reversed)
        prev_txid = vin.txid.hex()
        prev_raw = client.get_raw_tx(prev_txid)
        prev_tx = Transaction.parse(bytes.fromhex(prev_raw))
        prev_output = prev_tx.vout[vin.vout]
        if prev_output.script_pubkey.data == addr_script_bytes:
            spent += prev_output.value

    return received - spent


def estimate_address_count(wallet_id):
    """
    How many addresses a scan of this wallet is likely to check, for the
    progress bar. Every derived address (including the gap) is stored, so a
    re-scan checks about as many as last time; a first scan checks at least
    the gap limit on both chains.
    """
    with db.get_conn() as conn:
        stored = len(db.get_addresses_for_wallet(conn, wallet_id))
    return max(stored, 2 * GAP_LIMIT)


# ---------------------------------------------------------------------------
# Wallet scanning
# ---------------------------------------------------------------------------

def scan_wallet(wallet_id, xpub, progress=None):
    """
    Full scan of a wallet: derive addresses, fetch transactions, store results.

    progress: optional callback(addresses_checked, transactions_found), called
    after each address.

    Raises ValueError for a bad xpub and ServerUnavailable if Fulcrum can't be
    reached or drops the connection. Nothing is saved for a wallet whose scan
    fails part-way, so a dropped connection can't make a wallet look empty.
    """
    log.info("Scanning wallet %s (xpub: %s...)", wallet_id, xpub[:20])

    try:
        root_key = bip32.HDKey.from_base58(xpub)
    except Exception as exc:
        raise ValueError(f"invalid xpub ({exc})") from exc

    client = ElectrumClient(ELECTRUM_HOST, ELECTRUM_PORT)
    checked = 0
    tx_found = 0

    try:
        client.connect()

        with db.get_conn() as conn:
            for chain in (0, 1):  # 0=receiving, 1=change
                gap = 0
                idx = 0
                while gap < GAP_LIMIT:
                    address, addr_sc = _derive_address_and_script(
                        root_key, chain, idx
                    )
                    addr_script_bytes = addr_sc.data
                    sh = _scripthash(addr_sc)

                    addr_id = db.upsert_address(
                        conn, wallet_id, address, chain, idx
                    )

                    try:
                        history = client.get_history(sh)
                    except ElectrumError as exc:
                        log.warning(
                            "Electrum error for %s: %s", address, exc
                        )
                        history = []

                    if not history:
                        gap += 1
                    else:
                        gap = 0
                        for item in history:
                            tx_hash = item["tx_hash"]
                            height = item.get("height", 0)

                            # Get block timestamp
                            block_time = None
                            if height and height > 0:
                                try:
                                    block_time = client.get_block_time(height)
                                except ElectrumError:
                                    pass

                            # Parse raw tx and compute net value
                            try:
                                raw_hex = client.get_raw_tx(tx_hash)
                                value_sats = _net_value_for_address(
                                    client, raw_hex, addr_script_bytes
                                )
                            except ServerUnavailable:
                                raise
                            except Exception as exc:
                                log.warning(
                                    "Error processing tx %s: %s",
                                    tx_hash, exc,
                                )
                                continue

                            db.upsert_transaction(
                                conn,
                                addr_id,
                                tx_hash,
                                height if height > 0 else None,
                                block_time,
                                value_sats,
                            )
                            tx_found += 1

                        log.debug(
                            "Wallet %s chain=%d idx=%d address=%s txs=%d",
                            wallet_id, chain, idx, address, len(history),
                        )

                    idx += 1
                    checked += 1
                    if progress:
                        progress(checked, tx_found)

            db.touch_wallet_scanned(conn, wallet_id)

        log.info(
            "Wallet %s scan complete: %d addresses, %d transactions.",
            wallet_id, checked, tx_found,
        )

    finally:
        client.close()


def scan_all_wallets():
    """Scan every wallet stored in the database."""
    with db.get_conn() as conn:
        wallets = db.get_wallets(conn)
    for w in wallets:
        try:
            scan_wallet(w["id"], w["xpub"])
        except Exception as exc:
            log.exception("Error scanning wallet %s: %s", w["id"], exc)
