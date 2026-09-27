# Sh UserDataBackupMode Cli

ShUdbmCli is a utility for compatible SH EMP Platform feature phones. It communicates with the handset through the Sharp `SHBackUP` USB OBEX service and its AT serial interface.

## Features

- Display the phone model, software version, IMEI, and OBEX connection details.
- Back up or restore one `SRAM`, `NOR`, `S-AND`, `HISTORY`, or AMR object at a time.
- Generate an SD authentication file from the firmware version and IMEI.
- Query, enable, or disable the SH User Flag.

## Requirements

- Python 3.10 or newer
- `pyusb` and `tqdm`
- A working libusb backend

```bash
python3 -m pip install pyusb tqdm
```

### USB Serial Setup

Before using commands that communicate through the AT Command Port, add the
phone's USB identifiers to the supported-device configuration of your system's
USB Serial driver
Reload the driver or reconnect the phone after changing the configuration. Make
sure the phone is exposed as a serial device (for example, `/dev/cu.usbmodem*`
or `/dev/ttyUSB*`) before running `makeauthsd` or `shusrflag`. If automatic
detection selects the wrong port, pass the AT Command Port explicitly with
`--port`.

## Usage

Probe the connected phone:

```bash
python3 ShUdbmCli.py probe
```

Back up an object with an automatically generated filename:

```bash
python3 ShUdbmCli.py backup --object S-AND --output
```

Restore a backup file:

```bash
python3 ShUdbmCli.py restore \
  --input S-AND_20260927_120000.bin \
  --confirm ERASE-AND-RESTORE
```

Generate an SD authentication file in the tool directory:

```bash
python3 ShUdbmCli.py makeauthsd
```

Copy the generated authentication file to the SD card root manually.

Query or change the Sharp user flag through the AT port:

```bash
python3 ShUdbmCli.py shusrflag status
python3 ShUdbmCli.py shusrflag on
python3 ShUdbmCli.py shusrflag off
```

Use `--help` with the main command or any subcommand for all available options.

## Backup and Restore Workflows

The USB and SD card workflows are mutually exclusive. Do not leave an SD card containing a valid authentication file in the phone while using USB backup or restore. Conversely, do not attempt the SD card workflow while using the PC USB backup tool.

### 1. USB Backup or Restore

1. Start the phone normally in Normal Mode.
2. Connect to its Command Port and enable the SH User Flag:

   ```bash
   python3 ShUdbmCli.py shusrflag on
   ```

3. Completely power off the phone.
4. Make sure the phone does not contain an SD card with a valid authentication file.
5. Hold `0` + `6` + the power key to start the phone in User Data Backup Mode.
6. Use the PC tool to perform the required `backup` or `restore` operation.

### 2. SD Card Backup or Restore

1. Start the phone normally in Normal Mode.
2. Connect to its Command Port and enable the Sharp user flag:

   ```bash
   python3 ShUdbmCli.py shusrflag on
   ```

3. Generate the SD authentication file:

   ```bash
   python3 ShUdbmCli.py makeauthsd
   ```

4. Copy the generated authentication file to the root of the SD card.
5. Create the following case-sensitive directories and empty placeholder files
   on the SD card:

   ```text
   /SHARPDEBUG/
   ├── SRAM.DAT
   ├── THERMLOG.DAT
   └── FLASH/
       ├── sand00.dat
       ├── nor00.dat
       └── nand00.dat
   ```

   On macOS or Linux, replace `/Volumes/SDCARD` with the actual SD card mount
   point and run:

   ```bash
   SD_ROOT=/Volumes/SDCARD
   mkdir -p "$SD_ROOT/SHARPDEBUG/FLASH"
   touch "$SD_ROOT/SHARPDEBUG/SRAM.DAT" \
         "$SD_ROOT/SHARPDEBUG/THERMLOG.DAT" \
         "$SD_ROOT/SHARPDEBUG/FLASH/sand00.dat" \
         "$SD_ROOT/SHARPDEBUG/FLASH/nor00.dat" \
         "$SD_ROOT/SHARPDEBUG/FLASH/nand00.dat"
   ```

   These files must exist before starting the SD card dump. The phone writes
   the corresponding dump data into the placeholder files.
6. Safely eject the SD card and insert it into the phone.
7. Completely power off the phone.
8. Hold `0` + `6` + the power key to enter User Data Backup Mode.
9. Use the phone's keys to select and perform the desired backup or restore operation.

## Warning

Restore operations overwrite handset data. Keep a known-good backup and verify that the selected object and input file are correct before continuing. Changing the Sharp user flag also modifies handset state.

When performing FTL-related experiments, removing the battery directly is recommended instead of pressing the power key. Pressing the power key disables the User Flag. If broken the user data partition, it will bricked your device!
