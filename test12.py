import RPi.GPIO as GPIO
import time

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# Setup pins
GPIO.setup(23, GPIO.OUT)
GPIO.setup(24, GPIO.OUT)
GPIO.setup(4, GPIO.OUT)

# Enable motor (LOW)
GPIO.output(4, GPIO.LOW)
time.sleep(0.5)

print("Testing GPIO 23...")
for i in range(10):
    GPIO.output(23, GPIO.HIGH)
    print("HIGH")
    time.sleep(1)
    GPIO.output(23, GPIO.LOW)
    print("LOW")
    time.sleep(1)

GPIO.cleanup()
