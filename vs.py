#!/usr/bin/env python3

import os
import time
import logging

try:
    from gpiozero import DigitalOutputDevice
except Exception:
    DigitalOutputDevice = None

try:
    from gpiozero.pins.lgpio import LGPIOFactory
except Exception:
    LGPIOFactory = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class CompressionTestRig:
    """Compression test rig using gpiozero, matching the main firmware pattern."""

    EN_PIN = 4
    MS1_PIN = 27
    MS2_PIN = 22
    STEP_PIN = 23
    DIR_PIN = 24

    DIR_UP = 1
    DIR_DOWN = 0

    def __init__(self):
        logger.info("Initializing Compression Test Rig (gpiozero)...")

        if DigitalOutputDevice is None:
            raise RuntimeError("gpiozero is not installed. Install it with: sudo apt install python3-gpiozero")

        self.backend_name = "gpiozero"
        self.en_pin = None
        self.ms1_pin = None
        self.ms2_pin = None
        self.step_pin = None
        self.dir_pin = None

        try:
            self.en_pin = DigitalOutputDevice(self.EN_PIN)
            self.ms1_pin = DigitalOutputDevice(self.MS1_PIN)
            self.ms2_pin = DigitalOutputDevice(self.MS2_PIN)
            self.step_pin = DigitalOutputDevice(self.STEP_PIN)
            self.dir_pin = DigitalOutputDevice(self.DIR_PIN)
            logger.info("Using default gpiozero pin factory")

            self._set_microstep_full()
            self._set_step(False)
            self._set_direction(self.DIR_DOWN)
            time.sleep(0.1)

            self._enable_motor()
            time.sleep(0.5)
            logger.info("Motor ENABLED")

            print("=" * 60)
            print("COMPRESSION TEST RIG - Pi")
            print("Using GPIO backend:", self.backend_name)
            print("=" * 60)
            print()

        except PermissionError as exc:
            logger.error("GPIO access failed. Run the script with sudo: sudo python3 %s", __file__)
            raise
        except Exception as exc:
            logger.error(f"Initialization failed: {exc}")
            raise

    def _set_microstep_full(self):
        self.ms1_pin.off()
        self.ms2_pin.off()

    def _set_direction(self, value):
        self.dir_pin.value = bool(value)

    def _set_step(self, value):
        self.step_pin.value = bool(value)

    def _enable_motor(self):
        self.en_pin.off()

    def press_down(self, steps=600):
        """Press down."""
        try:
            self._set_direction(self.DIR_DOWN)
            time.sleep(0.1)

            logger.info(f"Pressing down {steps} steps")
            print(f"\n>>> PRESSING DOWN {steps} STEPS <<<\n")

            for i in range(steps):
                self._set_step(True)
                time.sleep(0.00025)
                self._set_step(False)
                time.sleep(0.00025)

                if (i + 1) % 100 == 0:
                    print(f"Position: {i + 1}")

            print("\nCOMPRESSION COMPLETE\n")
            logger.info("Compression complete")

        except Exception as exc:
            logger.error(f"Error: {exc}")

    def retract(self, steps=600):
        """Retract."""
        try:
            self._set_direction(self.DIR_UP)
            time.sleep(0.1)

            logger.info(f"Retracting {steps} steps")
            print(f"\n>>> RETRACTING {steps} STEPS <<<\n")

            for i in range(steps):
                self._set_step(True)
                time.sleep(0.00025)
                self._set_step(False)
                time.sleep(0.00025)

                if (i + 1) % 100 == 0:
                    print(f"Retracted: {i + 1}")

            print("\nFULLY RETRACTED\n")
            logger.info("Retraction complete")

        except Exception as exc:
            logger.error(f"Error: {exc}")

    def cleanup(self):
        """Clean up the GPIO state."""
        try:
            self._set_step(False)
            self.en_pin.off()
            self.en_pin.close()
            self.step_pin.close()
            self.dir_pin.close()
            self.ms1_pin.close()
            self.ms2_pin.close()
            logger.info("GPIO cleaned up")
        except Exception as exc:
            logger.error(f"Cleanup error: {exc}")


if __name__ == "__main__":
    rig = None
    try:
        rig = CompressionTestRig()
        print("Commands: p, press 800, r, retract 800, q\n")

        while True:
            cmd = input("compression> ").strip().lower()
            if cmd == 'p':
                rig.press_down(600)
            elif cmd.startswith('press '):
                rig.press_down(int(cmd.split()[1]))
            elif cmd == 'r':
                rig.retract(600)
            elif cmd.startswith('retract '):
                rig.retract(int(cmd.split()[1]))
            elif cmd == 'q':
                break
            else:
                if cmd:
                    print("Unknown command")

    except KeyboardInterrupt:
        print("\n\nInterrupted")

    finally:
        if rig:
            rig.cleanup()
