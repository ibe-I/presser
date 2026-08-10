#!/usr/bin/env python3

from gpiozero import LED
import time

# GPIO Pins from your motherboard firmware
STP = LED(23, active_high=False)  # Step on GPIO 23
DIR = LED(24, active_high=False)  # Direction on GPIO 24

print("=" * 50)
print("COMPRESSION TEST RIG - Pi4")
print("GPIO 23=STEP, GPIO 24=DIR")
print("=" * 50)
print()

def press_down(steps=600):
    DIR.off()  # Compress
    time.sleep(0.1)
    
    print("\n>>> PRESSING DOWN " + str(steps) + " STEPS <<<\n")
    
    for i in range(steps):
        STP.on()
        time.sleep(0.0005)
        STP.off()
        time.sleep(0.0005)
        
        if (i + 1) % 100 == 0:
            print("Position: " + str(i + 1))
    
    print("COMPRESSION COMPLETE\n")

def retract(steps=600):
    DIR.on()  # Retract
    time.sleep(0.1)
    
    print("\n>>> RETRACTING " + str(steps) + " STEPS <<<\n")
    
    for i in range(steps):
        STP.on()
        time.sleep(0.0005)
        STP.off()
        time.sleep(0.0005)
        
        if (i + 1) % 100 == 0:
            print("Retracted: " + str(i + 1))
    
    print("FULLY RETRACTED\n")

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
        elif cmd.startswith('retract '):
            steps = int(cmd.split()[1])
            retract(steps)
        elif cmd == 'q':
            print("Goodbye!\n")
            break
        else:
            print("Unknown command")

finally:
    STP.close()
    DIR.close()
    print("GPIO cleaned up")
