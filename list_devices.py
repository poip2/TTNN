import sounddevice as sd

devices = sd.query_devices()
print(f"Default input:  {sd.query_devices(kind='input')['name']}")
print(f"Default output: {sd.query_devices(kind='output')['name']}")
print()
for i, d in enumerate(devices):
    marker = ""
    if d["max_input_channels"] > 0:
        marker += " [IN]"
    if d["max_output_channels"] > 0:
        marker += " [OUT]"
    print(f"{i:2d}{marker:12s}  {d['name']}")
