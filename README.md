# Samsung ULD Manual Duplex Patcher

This script patches the official Samsung Unified Linux Driver (ULD) to add true **manual double-sided (duplex) printing** support for printers that lack hardware duplex capabilities (e.g., Samsung M2070 series).

Normally, the official Samsung ULD does not offer a "Manual Duplex" option in its CUPS PPD files, meaning you cannot easily print double-sided on Linux.

This patcher solves this by:
1. Adding "Double-Sided Printing" options to all compatible PPD files.
2. Replacing the default `rastertospl` binary with a lightweight Python wrapper.
3. Automatically splitting jobs into Odd and Even pages.
4. Using the printer's native **Manual Feed** pause for the even pages—allowing you to easily flip the stack, put it back, and press the physical **OK/Start button** on your printer to resume!

## How to Install

1. Download the official Samsung Unified Linux Driver (ULD) archive (e.g., [`uld_V1.00.39_01.17.tar.gz`](https://support.hp.com/ie-en/drivers/samsung-xpress-sl-m2070-laser-multifunction-printer-series/model/16450385)) from HP/Samsung's website.
2. Extract the driver archive:
   ```bash
   tar -zxvf uld_V1.00.39_01.17.tar.gz
   ```
3. Run this patcher script, pointing it to your extracted `uld` folder:
   ```bash
   python patch_uld.py /path/to/extracted/uld
   ```
4. The script will automatically inject the PPD duplex options and install the manual duplex wrapper.
5. Install your newly patched driver using its official installer:
   ```bash
   cd /path/to/extracted/uld
   sudo ./install.sh
   ```

## How to Print Double-Sided

1. Select **Double-Sided Printing** (Long Edge or Short Edge) in your print dialog.
2. The printer will print all the **Odd** pages first.
3. Once the odd pages finish, the printer will pause, its LED will blink, and it will prompt for "Manual Feed" (or "Load Paper" depending on the model).
4. **Take the stack of printed odd pages, flip it** (usually along the long edge), and place it back into the printer's paper tray.
5. **Press the physical OK or Start button on your printer.**
6. The printer will seamlessly print the remaining **Even** pages on the back!

## Requirements

* CUPS
* Python
* libcupsimage

> The patcher has been tested on the Samsung M2070 Series.