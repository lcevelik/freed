# FreeD Protocol Reader

A Python application that reads and parses camera tracking data from the FreeD (D1) protocol over UDP.

## Features

- Listens for FreeD protocol data on UDP socket
- Parses 29-byte FreeD D1 packets
- Validates checksums
- Displays camera tracking data in real-time
- Shows rotation (pan/tilt/roll), position (X/Y/Z), and lens data (zoom/focus)
- Tracks packet count and errors

## Requirements

- Python 3.6 or higher
- No external dependencies (uses only standard library)

## Configuration

The application is configured to listen on:
- **IP Address:** 0.0.0.0 (all network interfaces)
- **Port:** 40000 (default FreeD port)

You can modify these values in the `main()` function in [freed_reader.py](freed_reader.py) if needed.

## Usage

Run the application:

```bash
python freed_reader.py
```

The application will start listening for FreeD packets and display them in real-time.

To stop the application, press `Ctrl+C`.

## Output Format

When a valid FreeD packet is received, the application displays:

```
[Camera 1] From 192.168.1.100:40000
  Rotation:  Pan=    1234  Tilt=    5678  Roll=      90
  Position:  X=   10000  Y=   20000  Z=   30000
  Lens:      Zoom=    4000  Focus=    8000
  Packets: 42  Errors: 0
```

## FreeD Protocol Details

The FreeD (D1) protocol uses 29-byte UDP packets with the following structure:

- Byte 0: Message type (0xD1)
- Byte 1: Camera ID
- Bytes 2-4: Pan (24-bit signed integer)
- Bytes 5-7: Tilt (24-bit signed integer)
- Bytes 8-10: Roll (24-bit signed integer)
- Bytes 11-13: X position (24-bit signed integer)
- Bytes 14-16: Y position (24-bit signed integer)
- Bytes 17-19: Z position (24-bit signed integer)
- Bytes 20-22: Zoom (24-bit signed integer)
- Bytes 23-25: Focus (24-bit signed integer)
- Bytes 26-27: Spare (2 bytes)
- Byte 28: Checksum (XOR of all previous bytes)

## Troubleshooting

### No packets received

1. Verify the FreeD source is sending data to the correct IP and port
2. Check firewall settings allow UDP traffic on port 40000
3. Ensure no other application is using port 40000

### Invalid packets or checksum errors

1. Verify the source is sending FreeD D1 format (not D0 or other variants)
2. Check for network packet corruption
3. Verify the source is sending 29-byte packets

## Customization

You can extend the application by modifying the `display_data()` method to:
- Save data to a file
- Send data to another application
- Convert values to real-world units (degrees, meters, etc.)
- Filter or process specific camera IDs
