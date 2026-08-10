#!/usr/bin/env python3

from gpiozero import LED
import time

# GPIO Pins for YOUR motherboard
STP = LED(16, active_high=False)  # Step on GPIO 16
DIR = LED(18, active_high=False)  # Direction on GPIO 18

print("=" * 50)
print("COMPRESSION TEST RIG - Pi4")
print("GPIO 16=STP, GPIO 18=DIR")
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
