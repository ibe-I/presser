INFO:main:Initializing Compression Test Rig (gpiozero)...
WARNING:main:lgpio pin factory failed: 'can not open gpiochip'
/usr/lib/python3/dist-packages/gpiozero/devices.py:300: PinFactoryFallback: Falling back from lgpio: 'can not open gpiochip'
warnings.warn(
ERROR:main:GPIO initialization failed: Cannot determine SOC peripheral base address
Traceback (most recent call last):
File "/usr/lib/python3/dist-packages/gpiozero/pins/pi.py", line 411, in pin
pin = self.pins[info]
~~~~~~~~~^^^^^^
KeyError: PinInfo(number=7, name='GPIO4', names=frozenset({'4', 'BCM4', 'WPI7', 4, 'GPIO4', 'BOARD7', 'J8:7'}), pull='', row=4, col=1, interfaces=frozenset({'', 'dpi', 'spi', 'i2c', 'gpio', 'uart'}))

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
File "/home/presser/presser/vs.py", line 56, in init
self.en_pin = DigitalOutputDevice(self.EN_PIN)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
File "/usr/lib/python3/dist-packages/gpiozero/devices.py", line 108, in call
self = super().call(*args, **kwargs)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
File "/usr/lib/python3/dist-packages/gpiozero/output_devices.py", line 192, in init
super().init(pin, active_high=active_high,
File "/usr/lib/python3/dist-packages/gpiozero/output_devices.py", line 74, in init
super().init(pin, pin_factory=pin_factory)
File "/usr/lib/python3/dist-packages/gpiozero/mixins.py", line 75, in init
super().init(*args, **kwargs)
File "/usr/lib/python3/dist-packages/gpiozero/devices.py", line 553, in init
pin = self.pin_factory.pin(pin)
^^^^^^^^^^^^^^^^^^^^^^^^^
File "/usr/lib/python3/dist-packages/gpiozero/pins/pi.py", line 413, in pin
pin = self.pin_class(self, info)
^^^^^^^^^^^^^^^^^^^^^^^^^^
File "/usr/lib/python3/dist-packages/gpiozero/pins/rpigpio.py", line 101, in init
GPIO.setup(self._number, GPIO.IN, self.GPIO_PULL_UPS[self._pull])
RuntimeError: Cannot determine SOC peripheral base address

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
File "/home/presser/presser/vs.py", line 178, in
rig = CompressionTestRig()
^^^^^^^^^^^^^^^^^^^^
File "/home/presser/presser/vs.py", line 79, in init
raise RuntimeError(
RuntimeError: GPIO is not available on this Pi/OS combination. This usually means the kernel does not expose the GPIO peripheral to userspace.
