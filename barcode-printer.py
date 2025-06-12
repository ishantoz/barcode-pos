import os
import subprocess
import sys
import signal

def main():
    cwd = os.getcwd()

    # Determine the platform-specific path to the virtualenv's python
    if os.name == "nt":  # Windows
        python_path = os.path.join(cwd, ".venv", "Scripts", "python.exe")
    else:  # Unix-based (Linux, macOS)
        python_path = os.path.join(cwd, ".venv", "bin", "python")

    script_path = os.path.join(cwd, "main.py")

    if not os.path.exists(python_path):
        print(f"Error: Python not found at {python_path}")
        return 1
    if not os.path.exists(script_path):
        print(f"Error: Script not found at {script_path}")
        return 1

    process = None
    try:
        # Launch the script
        process = subprocess.Popen([python_path, script_path])

        # Wait for it to complete
        process.wait()
        return process.returncode

    except KeyboardInterrupt:
        print("\nExecution interrupted by user (Ctrl+C).")
        if process and process.poll() is None:
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
            except Exception as e:
                print(f"Error while terminating subprocess: {e}")
        return 130  # Convention: 128 + SIGINT

    except subprocess.SubprocessError as e:
        print(f"Subprocess error: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
