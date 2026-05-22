import sounddevice as sd

devs = sd.query_devices()
default = sd.query_devices(kind="input")["name"]

print()
print("Available microphones:")
print()
for i, d in enumerate(devs):
    if d["max_input_channels"] > 0:
        marker = " <-- default" if d["name"] == default else ""
        print(f"  [{i}] {d['name']}{marker}")
print()
print("To use a specific mic: set MICROPHONE_INDEX=<number> in .env")
print("Example: MICROPHONE_INDEX=1")
print()
input("Press Enter to close...")
