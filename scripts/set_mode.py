import sys
import os

def set_mode(target_mode):
    # Find .env relative to script location
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    env_path = os.path.join(project_root, ".env")
    
    if not os.path.exists(env_path):
        print(f"Error: {env_path} not found.")
        return

    # Valid modes
    valid_modes = ["paper", "testnet", "live"]
    if target_mode not in valid_modes:
        print(f"Error: Invalid mode '{target_mode}'. Choose from: {valid_modes}")
        return

    # Read current .env
    with open(env_path, 'r') as f:
        lines = f.readlines()

    current_mode = "unknown"
    private_key_set = False
    account_id_set = False
    live_confirmed = False

    for line in lines:
        if line.startswith("MODE="):
            current_mode = line.split("=")[1].strip()
        if line.startswith("PRIVATE_KEY=") and "your_evm" not in line:
            private_key_set = True
        if line.startswith("ACCOUNT_ID=") and "your_sodex" not in line:
            account_id_set = True
        if line.startswith("LIVE_MODE_CONFIRMED=true"):
            live_confirmed = True

    print(f"Current mode: {current_mode}")
    print(f"Switching to: {target_mode}")

    # Safety checks
    if target_mode == "live":
        print("\nWARNING: Live mode uses real money.")
        confirm = input("Type CONFIRM to proceed: ")
        if confirm != "CONFIRM":
            print("Aborting.")
            return

        print("\nChecking requirements...")
        if not private_key_set:
            print("✕ Error: PRIVATE_KEY not set in .env")
            return
        print("✓ PRIVATE_KEY set")
        
        if not account_id_set:
            print("✕ Error: ACCOUNT_ID not set in .env")
            return
        print("✓ ACCOUNT_ID set")
        
        if not live_confirmed:
            print("✕ Error: LIVE_MODE_CONFIRMED=true not found in .env")
            return
        print("✓ LIVE_MODE_CONFIRMED=true")

    elif target_mode == "testnet":
        if not private_key_set or not account_id_set:
            print("\nWARNING: Private Key or Account ID missing in .env. Connectivity might fail.")

    # Apply change
    new_lines = []
    found_mode = False
    for line in lines:
        if line.startswith("MODE="):
            new_lines.append(f"MODE={target_mode}\n")
            found_mode = True
        else:
            new_lines.append(line)
    
    if not found_mode:
        new_lines.insert(0, f"MODE={target_mode}\n")

    with open(env_path, 'w') as f:
        f.writelines(new_lines)

    print(f"✓ Mode set to {target_mode.upper()}")
    print("Run: python main.py")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/set_mode.py [paper|testnet|live]")
    else:
        set_mode(sys.argv[1].lower())
