#!/usr/bin/env python3

import RPi.GPIO as GPIO
import time

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# Setup pins as outputs
GPIO.setup(4, GPIO.OUT)   # EN
GPIO.setup(23, GPIO.OUT)  # STEP
GPIO.setup(24, GPIO.OUT)  # DIR

print("Testing with RPi.GPIO...")

# Try to enable motor
GPIO.output(4, GPIO.LOW)
print("EN set to LOW")
time.sleep(0.5)

# Set direction
GPIO.output(24, GPIO.LOW)
time.sleep(0.1)

# Send 100 steps
print("Sending 100 step pulses...")
for i in range(100):
    GPIO.output(23, GPIO.HIGH)
    time.sleep(0.0005)
    GPIO.output(23, GPIO.LOW)
    time.sleep(0.0005)

print("Done")
GPIO.cleanup()
