#!/usr/bin/env python3

import RPi.GPIO as GPIO
import time

# GPIO Pin Configuration
STP_PIN = 18  # Step input to encoder
DIR_PIN = 17  # Direction input to encoder

# Setup GPIO
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
GPIO.setup(STP_PIN, GPIO.OUT)
GPIO.setup(DIR_PIN, GPIO.OUT)

print("=" * 50)
print("COMPRESSION TEST RIG - Raspberry Pi 5")
print("=" * 50)
print()

def press_down(steps=600):
    """Press down for specified steps"""
    GPIO.output(DIR_PIN, GPIO.LOW)  # Compress direction
    time.sleep(0.1)
    
    print(f"\n>>> PRESSING DOWN {steps} STEPS <<<\n")
    
    for i in range(steps):
        GPIO.output(STP_PIN, GPIO.HIGH)
        time.sleep(0.0005)  # 500 microseconds
        GPIO.output(STP_PIN, GPIO.LOW)
        time.sleep(0.0005)
        
        # Progress every 100 steps
        if (i + 1) % 100 == 0:
            print(f"Position: {i + 1} / {steps}")
    
    print(f"\n✓ COMPRESSION COMPLETE")
    print("→ Read multimeter now")
    print("→ Type 'r' to retract\n")

def retract(steps=600):
    """Retract to home"""
    GPIO.output(DIR_PIN, GPIO.HIGH)  # Retract direction
    time.sleep(0.1)
    
    print(f"\n>>> RETRACTING {steps} STEPS <<<\n")
    
    for i in range(steps):
        GPIO.output(STP_PIN, GPIO.HIGH)
        time.sleep(0.0005)
        GPIO.output(STP_PIN, GPIO.LOW)
        time.sleep(0.0005)
        
        if (i + 1) % 100 == 0:
            print(f"Retracted: {i + 1} / {steps}")
    
    print(f"\n✓ FULLY RETRACTED\n")

def stop():
    """Stop all motion"""
    GPIO.output(STP_PIN, GPIO.LOW)
    GPIO.output(DIR_PIN, GPIO.LOW)
    print("\n!!! STOPPED !!!\n")

def help_menu():
    """Print help"""
    print("\nCOMMANDS:")
    print("  p          - Press down (600 steps)")
    print("  press 800  - Press down 800 steps")
    print("  r          - Retract to home (600 steps)")
    print("  retract 800- Retract 800 steps")
    print("  s          - Stop immediately")
    print("  h          - Help")
    print("  q          - Quit\n")

# Main loop
try:
    help_menu()
    
    while True:
        cmd = input("compression> ").strip().lower()
        
        if cmd == 'p':
            press_down(600)
        elif cmd.startswith('press '):
            try:
                steps = int(cmd.split()[1])
                press_down(steps)
            except:
                print("Usage: press 800")
        elif cmd == 'r':
            retract(600)
        elif cmd.startswith('retract '):
            try:
                steps = int(cmd.split()[1])
                retract(steps)
            except:
                print("Usage: retract 800")
        elif cmd == 's':
            stop()
        elif cmd == 'h':
            help_menu()
        elif cmd == 'q':
            print("Goodbye!\n")
            break
        else:
            print("Unknown command. Type 'h' for help")

except KeyboardInterrupt:
    print("\n\nInterrupted!")
    stop()

finally:
    GPIO.cleanup()
    print("GPIO cleaned up")
