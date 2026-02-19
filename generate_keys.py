"""
Generate an Ed25519 keypair for Robinhood Crypto API authentication.

Run this once, then:
  1. Copy the PUBLIC key to your Robinhood crypto account settings (web classic)
  2. Copy the PRIVATE key to config.py as BASE64_PRIVATE_KEY
  3. Copy the API key from Robinhood settings to config.py as API_KEY
"""

import nacl.signing
import base64

private_key = nacl.signing.SigningKey.generate()
public_key = private_key.verify_key

private_key_base64 = base64.b64encode(private_key.encode()).decode()
public_key_base64 = base64.b64encode(public_key.encode()).decode()

print("=" * 60)
print("Ed25519 Keypair Generated")
print("=" * 60)
print()
print("PRIVATE Key (keep secret, paste into config.py):")
print(private_key_base64)
print()
print("PUBLIC Key (paste into Robinhood account settings):")
print(public_key_base64)
print()
print("=" * 60)
print("NEXT STEPS:")
print("1. Go to your Robinhood crypto account settings on web classic")
print("2. Click 'Add key' and paste the PUBLIC key above")
print("3. Copy the API key Robinhood gives you")
print("4. Open config.py and set:")
print('   API_KEY = "your-api-key-from-robinhood"')
print(f'   BASE64_PRIVATE_KEY = "{private_key_base64}"')
print("=" * 60)
