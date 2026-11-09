# test_login.py
# Script to verify Kite login and connection

from login import get_kite
import sys

def check_kite_login():
    """
    Test if KiteConnect login is working properly
    """
    try:
        print("Attempting to connect to Kite API...")
        kite = get_kite()
        
        # Test 1: Check if connection is valid by fetching user profile
        print("\n[Test 1] Fetching user profile...")
        profile = kite.profile()
        
        print("[OK] Login Successful!")
        print(f"  User ID: {profile.get('user_id')}")
        print(f"  User Name: {profile.get('user_name')}")
        print(f"  Email: {profile.get('email')}")
        print(f"  Broker: {profile.get('broker')}")

        print("\n[Test 2] Fetching account margins...")
        kite.margins()
        print("[OK] Authenticated account request succeeded.")
        
        # Test 3: Check if we can fetch instruments
        # print("\n[Test 2] Fetching instruments list...")
        # instruments = kite.instruments()
        # print(f"[OK] Total instruments available: {len(instruments)}")
        
        # Test 3: Check if we can fetch account balance
        # print("\n[Test 3] Fetching account balance...")
        # margins = kite.margins()
        # available = margins.get('available', {})
        # print(f"[OK] Available Cash: Rs {available.get('cash', 0)}")
        # print(f"  Available Margin: ₹{available.get('margin', 0)}")
        
        print("\n" + "="*50)
        print("[OK] All tests passed! Kite login is working correctly.")
        print("="*50)
        return True
        
    except Exception as e:
        print("\n[FAIL] Login Failed!")
        print(f"Error Type: {type(e).__name__}")
        print(f"Error Message: {str(e)}")
        
        print("\nPossible reasons:")
        print("  1. ACCESS_TOKEN is expired or invalid")
        print("  2. API_KEY is incorrect or inactive")
        print("  3. No internet connection")
        print("  4. Zerodha API is down")
        
        print("\nSolution:")
        print("  Run: python generate_token.py")
        print("  Then update the ACCESS_TOKEN in .env")
        
        return False

if __name__ == "__main__":
    success = check_kite_login()
    sys.exit(0 if success else 1)
