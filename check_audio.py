import sounddevice as sd
devs = sd.query_devices()
print('All audio devices:')
for i, d in enumerate(devs):
    kind = 'IN ' if d['max_input_channels'] > 0 else 'OUT'
    print(f'  [{i}] {kind} {d["name"]}')
print()
try:
    d1 = sd.query_devices(1, kind='input')
    print('Device 1 (input):', d1['name'])
except Exception as e:
    print('Device 1 error:', e)
