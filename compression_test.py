import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time

try:
    from gpiozero import DigitalOutputDevice
except Exception:
    DigitalOutputDevice = None

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None


class StepperController:
    """Reference controller that mirrors the GPIO layout from the working firmware."""

    def __init__(self):
        # Same pins as the firmware reference
        self.EN_PIN = 4
        self.MS1_PIN = 27
        self.MS2_PIN = 22
        self.STEP_PIN = 23
        self.DIR_PIN = 24

        self.stop_event = threading.Event()
        self.thread = None
        self.running = False
        self.step_delay = 0.002
        self.use_gpiozero = False

        if DigitalOutputDevice is not None:
            self._init_gpiozero()
        elif GPIO is not None:
            self._init_rpi_gpio()
        else:
            raise RuntimeError("No GPIO backend available. Run this on a Raspberry Pi with gpiozero or RPi.GPIO installed.")

    def _init_gpiozero(self):
        self.use_gpiozero = True
        self.en_pin = DigitalOutputDevice(self.EN_PIN)
        self.ms1_pin = DigitalOutputDevice(self.MS1_PIN)
        self.ms2_pin = DigitalOutputDevice(self.MS2_PIN)
        self.step_pin = DigitalOutputDevice(self.STEP_PIN)
        self.dir_pin = DigitalOutputDevice(self.DIR_PIN)

        self.set_microstep("Full")
        self.enable_motor()

    def _init_rpi_gpio(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in [self.EN_PIN, self.MS1_PIN, self.MS2_PIN, self.STEP_PIN, self.DIR_PIN]:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

        self.set_microstep("Full")
        self.enable_motor()

    def set_microstep(self, mode):
        modes = {
            "Full": (0, 0),
            "Half": (1, 0),
            "Quarter": (0, 1),
            "Eighth": (1, 1),
        }
        if mode not in modes:
            raise ValueError("Unsupported microstep mode")

        ms1, ms2 = modes[mode]
        if self.use_gpiozero:
            self.ms1_pin.value = bool(ms1)
            self.ms2_pin.value = bool(ms2)
        else:
            GPIO.output(self.MS1_PIN, ms1)
            GPIO.output(self.MS2_PIN, ms2)

    def enable_motor(self):
        if self.use_gpiozero:
            self.en_pin.off()
        else:
            GPIO.output(self.EN_PIN, GPIO.LOW)

    def disable_motor(self):
        if self.use_gpiozero:
            self.en_pin.off()
        else:
            GPIO.output(self.EN_PIN, GPIO.LOW)

    def set_direction(self, clockwise=True):
        if self.use_gpiozero:
            if clockwise:
                self.dir_pin.on()
            else:
                self.dir_pin.off()
        else:
            GPIO.output(self.DIR_PIN, GPIO.HIGH if clockwise else GPIO.LOW)

    def _set_step_low(self):
        if self.use_gpiozero:
            self.step_pin.off()
        else:
            GPIO.output(self.STEP_PIN, GPIO.LOW)

    def _step_pulse(self):
        if self.use_gpiozero:
            self.step_pin.on()
            time.sleep(self.step_delay)
            self.step_pin.off()
            time.sleep(self.step_delay)
        else:
            GPIO.output(self.STEP_PIN, GPIO.HIGH)
            time.sleep(self.step_delay)
            GPIO.output(self.STEP_PIN, GPIO.LOW)
            time.sleep(self.step_delay)

    def move_steps(self, steps, speed_hz=500, clockwise=True):
        if self.running:
            self.stop()

        self.stop_event.clear()
        self.running = True
        self.thread = threading.Thread(
            target=self._move_worker,
            args=(steps, speed_hz, clockwise),
            daemon=True,
        )
        self.thread.start()

    def _move_worker(self, steps, speed_hz, clockwise):
        try:
            self.enable_motor()
            self.set_direction(clockwise)

            step_count = abs(int(steps))
            if step_count <= 0:
                return

            self.step_delay = max(0.001, 1.0 / max(speed_hz, 1))
            for _ in range(step_count):
                if self.stop_event.is_set():
                    break
                self._step_pulse()
        finally:
            self.running = False
            self.stop_event.set()
            self.disable_motor()
            self._set_step_low()

    def stop(self):
        self.stop_event.set()
        self._set_step_low()
        self.disable_motor()
        self.running = False

    def cleanup(self):
        self.stop()
        if self.use_gpiozero:
            for pin in [self.en_pin, self.ms1_pin, self.ms2_pin, self.step_pin, self.dir_pin]:
                try:
                    pin.close()
                except Exception:
                    pass
        else:
            GPIO.cleanup()


class StepperGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Stepper Motor Controller")
        self.root.geometry("480x320")

        self.controller = StepperController()
        self.steps_var = tk.StringVar(value="200")
        self.speed_var = tk.IntVar(value=500)
        self.direction_var = tk.StringVar(value="CW")
        self.microstep_var = tk.StringVar(value="Full")
        self.status_var = tk.StringVar(value="Ready")

        self.build_ui()

    def build_ui(self):
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Steps").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.steps_var, width=12).grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="Speed (Hz)").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Scale(frame, from_=50, to=3000, variable=self.speed_var, orient="horizontal").grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(frame, textvariable=self.speed_var).grid(row=1, column=2, padx=8)

        ttk.Label(frame, text="Direction").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Combobox(frame, textvariable=self.direction_var, values=["CW", "CCW"], state="readonly", width=10).grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="Microstep").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Combobox(frame, textvariable=self.microstep_var, values=["Full", "Half", "Quarter", "Eighth"], state="readonly", width=12).grid(row=3, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="Status").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Label(frame, textvariable=self.status_var, foreground="blue").grid(row=4, column=1, sticky="w", pady=(8, 0))

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=5, column=0, columnspan=3, pady=10)

        ttk.Button(btn_frame, text="Move", command=self.on_move).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Stop", command=self.on_stop).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Test 1 Step", command=self.on_test_step).pack(side="left", padx=5)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def on_move(self):
        try:
            steps = int(self.steps_var.get())
        except ValueError:
            messagebox.showerror("Error", "Please enter a valid number of steps")
            return

        speed = int(self.speed_var.get())
        clockwise = self.direction_var.get() == "CW"
        mode = self.microstep_var.get()

        try:
            self.controller.set_microstep(mode)
            self.status_var.set("Moving...")
            self.controller.move_steps(steps, speed_hz=speed, clockwise=clockwise)
        except Exception as exc:
            self.status_var.set("Error")
            messagebox.showerror("Error", str(exc))

    def on_test_step(self):
        try:
            self.controller.set_microstep(self.microstep_var.get())
            self.controller.set_direction(self.direction_var.get() == "CW")
            self.controller.enable_motor()
            self.controller._step_pulse()
            self.controller.disable_motor()
            self.status_var.set("Test step sent")
        except Exception as exc:
            self.status_var.set("Error")
            messagebox.showerror("Error", str(exc))

    def on_stop(self):
        self.controller.stop()
        self.status_var.set("Stopped")

    def on_close(self):
        self.controller.stop()
        self.controller.cleanup()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = StepperGUI(root)
    root.mainloop()
