#!/usr/bin/env python3

import time
from gpiozero import DigitalOutputDevice
import sys

print("=" * 60)
print("COMPRESSION RIG DIAGNOSTIC TEST")
print("=" * 60)
print()

# Test each pin individually
pins = {
    'EN (4)': 4,
    'MS1 (27)': 27,
    'MS2 (22)': 22,
    'STEP (23)': 23,
    'DIR (24)': 24
}

print("Testing each GPIO pin individually...")
print("Measure voltage on each wire as it tests\n")

try:
    for name, pin_num in pins.items():
        print(f"\n--- Testing {name} ---")
        pin = DigitalOutputDevice(pin_num)
        
        # Set OFF
        pin.off()
        print(f"PIN OFF: {name} should be LOW (0V)")
        input("  Measured voltage? Press Enter...")
        
        # Set ON
        pin.on()
        print(f"PIN ON: {name} should be HIGH (3.3V)")
        input("  Measured voltage? Press Enter...")
        
        # Toggle 5 times
        print(f"Toggling {name} 5 times...")
        for i in range(5):
            pin.on()
            time.sleep(0.2)
            pin.off()
            time.sleep(0.2)
        
        pin.off()
        pin.close()
        print(f"  Done with {name}\n")

except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)

print("\n" + "=" * 60)
print("DIAGNOSTIC COMPLETE")
print("=" * 60)
print("\nREPORT:")
print("- Which pins toggled properly? (0V ↔ 3.3V)")
print("- Which pins stayed stuck?")
print("- Did motor spin at any point?")
print()
