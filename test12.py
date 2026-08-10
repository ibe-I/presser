sudo python3 << EOF
from gpiozero import LED
import time

TEST_PIN = LED(12)

print("GPIO 12 will toggle 10 times")
print("Use voltmeter on the wire connected to GPIO 12!")
print()

for i in range(10):
    TEST_PIN.on()
    print(f"{i+1}. HIGH - voltmeter should show 3.3V")
    time.sleep(1)
    TEST_PIN.off()
    print(f"{i+1}. LOW - voltmeter should show 0V")
    time.sleep(1)

TEST_PIN.close()
EOF
