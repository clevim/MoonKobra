#!/usr/bin/env python3
"""
fetch_credentials.py – Fetches and decrypts the Anycubic Kobra X credentials through the HTTP API.

Original approach by bebu (KX-Bridge PR #19).
Obtained by reverse engineering the Vue project embedded in libWorkbench.so (Anycubic Slicer Next).
Does not need the slicer running — only the printer's IP on the LAN.

Algorithm: AES-256-CBC
  Key: token[16:32] of the /info response
  IV:  token of the /ctrl response

Usage:
    python3 fetch_credentials.py --ip 192.168.x.x
    python3 fetch_credentials.py --ip 192.168.x.x --write-config
    python3 fetch_credentials.py --ip 192.168.x.x --write-config --config-file config/config.ini
    python3 fetch_credentials.py --ctrl ctrl.json --info info.json
"""

import json
import sys
import base64
import hashlib
import argparse
import os
import time
import random
import string
import requests
from pathlib import Path
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


def evp_bytes_to_key(password, salt, key_len, iv_len):
    """
    Derives key and IV from password and salt using OpenSSL's EVP_BytesToKey
    Mimics CryptoJS's default key derivation
    """
    m = []
    i = 0
    while len(b''.join(m)) < (key_len + iv_len):
        md5 = hashlib.md5()
        data = password + salt
        if i > 0:
            data = m[i - 1] + password + salt
        md5.update(data)
        m.append(md5.digest())
        i += 1
    ms = b''.join(m)
    return ms[:key_len], ms[key_len:key_len + iv_len]


def generate_signature(token, ts, nonce):
    """
    Generates the MD5 signature for the /ctrl endpoint
    Signature = md5(md5(token[0:16]) + ts + nonce)
    """
    # First MD5: token.slice(0, 16)
    first_md5 = hashlib.md5(token[:16].encode('utf-8')).hexdigest()
    # Second MD5: first_md5 + ts + nonce
    signature_data = first_md5 + str(ts) + nonce
    signature = hashlib.md5(signature_data.encode('utf-8')).hexdigest()
    return signature


def generate_nonce(length=6):
    """Generates a random alphanumeric nonce"""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))


def fetch_from_http(ip, port, endpoint, token=None, did="random", verbose=False):
    """
    Fetches data from a printer HTTP endpoint
    
    Args:
        ip (str): printer IP address
        port (int): port number (default 18910)
        endpoint (str): 'info' or 'ctrl'
        token (str): device token (required for the /ctrl endpoint)
        did (str): device ID (required for the /ctrl endpoint)
        verbose (bool): prints debug information
    
    Returns:
        dict: JSON data of the response
    """
    try:
        if endpoint == 'info':
            url = f"http://{ip}:{port}/info"
            if verbose:
                print(f"Fetching: {url}")
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.json()
        
        elif endpoint == 'ctrl':
            if not token:
                raise ValueError("Token is required for the /ctrl endpoint")
            
            # Builds the signature parameters
            ts = int(time.time() * 1000)  # current timestamp in ms
            nonce = generate_nonce(6)
            signature = generate_signature(token, ts, nonce)
            
            url = f"http://{ip}:{port}/ctrl"
            params = {
                'ts': ts,
                'nonce': nonce,
                'sign': signature,
                'did': did
            }
            
            if verbose:
                print(f"Fetching: {url}")
                print("  Parameters:")
                print(f"    ts: {ts}")
                print(f"    nonce: {nonce}")
                print(f"    sign: {signature}")
                print(f"    did: {did}")
            
            response = requests.post(url, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        
        else:
            raise ValueError(f"Unknown endpoint: {endpoint}")
    
    except requests.exceptions.RequestException as e:
        raise Exception(f"HTTP request failed for {endpoint}: {e}")
    except json.JSONDecodeError as e:
        raise Exception(f"Invalid JSON response from {endpoint}: {e}")


def decrypt_text(encrypted_data, key, iv):
    """
    Decrypts AES-256-CBC data
    Handles CryptoJS-style ciphertext (OpenSSL format with salt)
    
    Args:
        encrypted_data (str): encrypted data string (CryptoJS format)
        key (str): decryption key string
        iv (str): initialization vector string
    
    Returns:
        dict: decrypted JSON data
    """
    try:
        # Converts key and IV to bytes
        key_bytes = key.encode('utf-8')
        iv_bytes = iv.encode('utf-8')
        
        # Decrypts with the key and IV directly (as in the original code)
        cipher = AES.new(key_bytes, AES.MODE_CBC, iv_bytes)
        
        # encrypted_data may be base64 or hex
        # Try base64 first
        try:
            encrypted_bytes = base64.b64decode(encrypted_data)
        except:
            try:
                # Try as hex
                encrypted_bytes = bytes.fromhex(encrypted_data)
            except:
                # If everything fails, encode as UTF-8
                encrypted_bytes = encrypted_data.encode('utf-8')
        
        # Decrypt
        decrypted = cipher.decrypt(encrypted_bytes)
        
        # Try to remove the padding
        try:
            unpadded = unpad(decrypted, AES.block_size)
        except ValueError:
            # If removing the padding fails, use it as is
            unpadded = decrypted
        
        plaintext = unpadded.decode('utf-8')
        
        # Parse the JSON
        return json.loads(plaintext)
    
    except Exception as e:
        return {"error": str(e), "error_type": type(e).__name__}


def main():
    """Main function to decrypt the printer data"""
    
    # Parse the command line arguments
    parser = argparse.ArgumentParser(
        description='Fetch and decrypt Anycubic Kobra X credentials via HTTP API',
    )
    # HTTP mode
    parser.add_argument('--ip', help='IP address of the printer')
    parser.add_argument('--port', type=int, default=18910, help='Printer HTTP port (default: 18910)')

    # File mode
    parser.add_argument('--ctrl', default='ctrl.json', help='Path to ctrl.json (default: ctrl.json)')
    parser.add_argument('--info', default='info.json', help='Path to info.json (default: info.json)')

    # Output
    parser.add_argument('--output', default=None, help='Save raw decrypted JSON to file (optional)')
    parser.add_argument('--write-config', action='store_true',
                        help='Write credentials to config.ini')
    parser.add_argument('--config-file', default=None,
                        help='Path to config.ini (default: config/config.ini next to this script)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')

    args = parser.parse_args()
    
    # Pick the mode: HTTP or file
    if args.ip:
        # HTTP mode: fetch from the printer
        if args.verbose:
            print("=" * 70)
            print("Fetching the configuration from the printer over HTTP")
            print("=" * 70)
            print(f"Printer IP: {args.ip}:{args.port}")
            print()
        
        try:
            # Fetch info.json
            if args.verbose:
                print("Step 1: fetching device information...")
            info = fetch_from_http(args.ip, args.port, 'info', verbose=args.verbose)
            
            # Take the token from info
            token = info.get('token')
            if not token:
                print("Error: no token in the /info response", file=sys.stderr)
                return 1
            
            # Fetch data.json (encrypted config) from the /ctrl endpoint
            if args.verbose:
                print("\nStep 2: fetching the encrypted configuration from /ctrl...")
            data = fetch_from_http(args.ip, args.port, 'ctrl', token=token, verbose=args.verbose)
            
            if args.verbose:
                print("\nData fetched successfully!")
            
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    
    else:
        # File mode: load from disk
        if args.verbose:
            print("=" * 70)
            print("Loading the configuration from files")
            print("=" * 70)
        
        # Check that the input files exist
        if not Path(args.ctrl).exists():
            print(f"Error: {args.ctrl} not found", file=sys.stderr)
            return 1
        if not Path(args.info).exists():
            print(f"Error: {args.info} not found", file=sys.stderr)
            return 1
        
        # Read ctrl.json
        try:
            with open(args.ctrl, 'r') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error reading {args.ctrl}: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Error reading {args.ctrl}: {e}", file=sys.stderr)
            return 1
        
        # Read info.json
        try:
            with open(args.info, 'r') as f:
                info = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error reading {args.info}: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Error reading {args.info}: {e}", file=sys.stderr)
            return 1
    
    # Extract the values
    try:
        encrypted_info = data['data']['info']
        response_token = data['data']['token']
        full_token = info['token']
    except KeyError as e:
        print(f"Error: missing required key {e}", file=sys.stderr)
        return 1
    
    # Build the decryption key and IV
    key_part = full_token[16:32]
    
    if args.verbose:
        print("=" * 70)
        print("Decrypting the printer configuration")
        print("=" * 70)
        print(f"Input data file: {args.ctrl}")
        print(f"Input info file: {args.info}")
        print(f"Output file: {args.output}")
        print()
        print("Decryption parameters:")
        print(f"  Encrypted data size: {len(encrypted_info)} bytes")
        print(f"  Full token: {full_token}")
        print(f"  Full token length: {len(full_token)} characters")
        print(f"  Response token (IV): {response_token}")
        print(f"  Decryption key: {key_part}")
        print(f"  Key length: {len(key_part)} characters")
        print(f"  IV length: {len(response_token)} characters")
        print()
    
    # Decrypt
    if args.verbose:
        print("Decrypting...")
    
    result = decrypt_text(encrypted_info, key_part, response_token)
    
    if 'error' in result:
        print(f"Decryption error: {result.get('error')}", file=sys.stderr)
        return 1
    
    # Show the result
    print()
    print("=" * 55)
    print("  CREDENTIALS")
    print("=" * 55)
    print(f"  {'Printer IP':12s}  {result.get('ip', 'n/a')}")
    print(f"  {'Username':12s}  {result.get('username', 'n/a')}")
    print(f"  {'Password':12s}  {result.get('password', 'n/a')}")
    print(f"  {'Device-ID':12s}  {result.get('deviceId', 'n/a')}")
    print(f"  {'Mode-ID':12s}  {result.get('modeId', 'n/a')}")
    print(f"  {'Model':12s}  {result.get('modelName', 'n/a')}")
    print(f"  {'Broker':12s}  {result.get('broker', 'n/a')}")
    print("=" * 55)

    if args.verbose:
        print()
        print("Full decrypted config:")
        # Leave certificates/keys out of the verbose output so the terminal stays readable
        display = {k: v for k, v in result.items() if k not in ('devicecrt', 'devicepk')}
        print(json.dumps(display, indent=2))

    # Optionally save the raw JSON
    if args.output:
        try:
            with open(args.output, 'w') as f:
                json.dump(result, f, indent=2)
            print(f"\nRaw config saved to: {args.output}")
        except Exception as e:
            print(f"Error writing to {args.output}: {e}", file=sys.stderr)
            return 1

    # Write config.ini
    if args.write_config:
        if args.config_file:
            config_path = args.config_file
        else:
            config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       'config', 'config.ini')
            config_path = os.path.normpath(config_path)

        _write_config_ini(result, config_path)
    else:
        print("\nTip: use --write-config to write the credentials straight into config.ini")

    return 0


def _write_config_ini(result: dict, config_path: str):
    """Writes the fetched credentials to config.ini, keeping the existing non-credential keys."""
    import configparser

    cfg = configparser.ConfigParser()

    if os.path.isfile(config_path):
        cfg.read(config_path)

    if not cfg.has_section('connection'):
        cfg.add_section('connection')

    cfg.set('connection', 'printer_ip', result.get('ip', ''))
    cfg.set('connection', 'mqtt_port',  '9883')
    cfg.set('connection', 'username',   result.get('username', ''))
    cfg.set('connection', 'password',   result.get('password', ''))
    cfg.set('connection', 'device_id',  result.get('deviceId', ''))
    cfg.set('connection', 'mode_id',    result.get('modeId', '20030'))

    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, 'w') as f:
        cfg.write(f)

    print(f"\n✓ Credentials written to '{config_path}'.")


if __name__ == '__main__':
    sys.exit(main())
