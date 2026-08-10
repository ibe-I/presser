#!/usr/bin/env python3

from gpiozero import LED
import time

# GPIO Pins - active_high=False means inverted logic
STP = LED(18, active_high=False)
DIR = LED(17, active_high=False)

print("=" * 50)
print("COMPRESSION TEST RIG - Pi5")
print("=" * 50)
print()

def press_down(steps=600):
    DIR.off()  # Compress
    time.sleep(0.1)
    
    print(f"\n>>> PRESSING DOWN {steps} STEPS <<<\n")
    
    for i in range(steps):
        STP.on()
        time.sleep(0.0005)
        STP.off()
        time.sleep(0.0005)
        
        if (i + 1) % 100 == 0:
            print(f"Position: {i + 1}")
    
    print("✓ COMPRESSION COMPLETE\n")

def retract(steps=600):
    DIR.on()  # Retract
    time.sleep(0.1)
    
    print(f"\n>>> RETRACTING {steps} STEPS <<<\n")
    
    for i in range(steps):
        STP.on()
        time.sleep(0.0005)
        STP.off()
        time.sleep(0.0005)
        
        if (i + 1) % 100 == 0:
            print(f"Retracted: {i + 1}")
    
    print("✓ FULLY RETRACTED\n")

try:
    print("Commands: p=press, r=retract, q=quit\n")
    
    while True:
        cmd = input("compression> ").strip().lower()
        
        if cmd == 'p':
            press_down(600)
        elif cmd.startswith('press '):
            steps = int(cmd.split()[1])
            press_down(steps)
        elif cmd == 'r':
            retract(600)
        elif cmd == 'q':
            print("Goodbye!\n")
            break

finally:
    STP.close()
    DIR.close()
    print("GPIO cleaned up")
