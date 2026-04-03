import base58

def format_hwid(hex_hwid_str: str) -> str:
    # Convert hex string to bytes
    byte_data = bytes.fromhex(hex_hwid_str)
    
    # Encode bytes to Base58
    base58_string = base58.b58encode(byte_data).decode('utf-8')
    
    return base58_string