#!/usr/bin/env python3

from gpiozero import DigitalOutputDevice
import time

# Initialize GPIO pins (same as printer)
en_pin = DigitalOutputDevice(4)      # Enable
ms1_pin = DigitalOutputDevice(27)    # Microstepping 1
ms2_pin = DigitalOutputDevice(22)    # Microstepping 2
step_pin = DigitalOutputDevice(23)   # Step
dir_pin = DigitalOutputDevice(24)    # Direction

print("=" * 50)
print("COMPRESSION TEST RIG - Pi4")
print("=" * 50)

# Set microstepping to full step
ms1_pin.off()
ms2_pin.off()

# Enable motor (LOW enables)
en_pin.off()
time.sleep(0.5)
print("Motor enabled\n")

def press_down(steps=600):
    dir_pin.off()  # Compress direction
    time.sleep(0.1)
    
    print("\n>>> PRESSING DOWN " + str(steps) + " STEPS <<<\n")
    
    for i in range(steps):
        step_pin.on()
        time.sleep(0.0005)
        step_pin.off()
        time.sleep(0.0005)
        
        if (i + 1) % 100 == 0:
            print("Position: " + str(i + 1))
    
    print("COMPRESSION COMPLETE\n")

def retract(steps=600):
    dir_pin.on()  # Retract direction
    time.sleep(0.1)
    
    print("\n>>> RETRACTING " + str(steps) + " STEPS <<<\n")
    
    for i in range(steps):
        step_pin.on()
        time.sleep(0.0005)
        step_pin.off()
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
    en_pin.close()
    step_pin.close()
    dir_pin.close()
    print("GPIO cleaned up")
