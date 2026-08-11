#!/usr/bin/env python3

import gpiod
import time
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CompressionTestRig:
    """Compression test rig - libgpiod for Pi5"""
    
    def __init__(self):
        logger.info("Initializing for Pi5 using libgpiod...")
        
        try:
            # Open gpiochip0 (Pi5 GPIO controller)
            self.chip = gpiod.Chip("gpiochip0")
            
            # Request lines
            self.en_line = self.chip.get_line(4)      # EN
            self.step_line = self.chip.get_line(23)   # STEP
            self.dir_line = self.chip.get_line(24)    # DIR
            self.ms1_line = self.chip.get_line(27)    # MS1
            self.ms2_line = self.chip.get_line(22)    # MS2
            
            # Configure as outputs
            self.en_line.request(consumer="compression", type=gpiod.LINE_REQ_DIR_OUT)
            self.step_line.request(consumer="compression", type=gpiod.LINE_REQ_DIR_OUT)
            self.dir_line.request(consumer="compression", type=gpiod.LINE_REQ_DIR_OUT)
            self.ms1_line.request(consumer="compression", type=gpiod.LINE_REQ_DIR_OUT)
            self.ms2_line.request(consumer="compression", type=gpiod.LINE_REQ_DIR_OUT)
            
            # Set initial states
            self.ms1_line.set_value(0)  # Full step
            self.ms2_line.set_value(0)
            self.step_line.set_value(0)
            self.dir_line.set_value(0)
            
            # Enable motor (0 = enable)
            self.en_line.set_value(0)
            time.sleep(0.5)
            logger.info("Motor ENABLED on Pi5")
            
            print("=" * 60)
            print("COMPRESSION TEST RIG - Pi5 (libgpiod)")
            print("=" * 60)
            print()
            
        except Exception as e:
            logger.error(f"Init failed: {e}")
            raise
    
    def press_down(self, steps=600):
        """Press down"""
        try:
            self.dir_line.set_value(0)  # DIR_DOWN
            time.sleep(0.1)
            
            print(f"\n>>> PRESSING {steps} STEPS <<<\n")
            
            for i in range(steps):
                self.step_line.set_value(1)
                time.sleep(0.00025)
                self.step_line.set_value(0)
                time.sleep(0.00025)
                
                if (i + 1) % 100 == 0:
                    print(f"Position: {i + 1}")
            
            print("\nCOMPRESSION COMPLETE\n")
        
        except Exception as e:
            logger.error(f"Error: {e}")
    
    def retract(self, steps=600):
        """Retract"""
        try:
            self.dir_line.set_value(1)  # DIR_UP
            time.sleep(0.1)
            
            print(f"\n>>> RETRACTING {steps} STEPS <<<\n")
            
            for i in range(steps):
                self.step_line.set_value(1)
                time.sleep(0.00025)
                self.step_line.set_value(0)
                time.sleep(0.00025)
                
                if (i + 1) % 100 == 0:
                    print(f"Retracted: {i + 1}")
            
            print("\nFULLY RETRACTED\n")
        
        except Exception as e:
            logger.error(f"Error: {e}")
    
    def cleanup(self):
        """Cleanup"""
        try:
            self.en_line.set_value(1)  # Disable
            self.en_line.release()
            self.step_line.release()
            self.dir_line.release()
            self.ms1_line.release()
            self.ms2_line.release()
            logger.info("GPIO released")
        except:
            pass


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
    except KeyboardInterrupt:
        print("\nInterrupted")
    finally:
        if rig:
            rig.cleanup()
