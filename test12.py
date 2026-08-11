#!/usr/bin/env python3

from gpiozero import DigitalOutputDevice
import time

# Initialize
en_pin = DigitalOutputDevice(4)
step_pin = DigitalOutputDevice(23)
dir_pin = DigitalOutputDevice(24)

print("Testing motor activation...")

# Enable motor using .value assignment
en_pin.value = 0  # Try direct assignment
time.sleep(0.5)

# Set direction
dir_pin.value = 0
time.sleep(0.1)

# Send 100 step pulses
print("Sending 100 step pulses...")
for i in range(100):
    step_pin.on()
    time.sleep(0.0005)
    step_pin.off()
    time.sleep(0.0005)

print("Done")

en_pin.close()
step_pin.close()
dir_pin.close()
