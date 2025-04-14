#!/usr/bin/env python

import wx
import wx.adv
import wx.lib.inspection
import wx.lib.mixins.inspection

import sys
import os
import esptool
import threading
import json
import images as images
from serial import SerialException
from serial.tools import list_ports
import locale

# see https://discuss.wxpython.org/t/wxpython4-1-1-python3-8-locale-wxassertionerror/35168
locale.setlocale(locale.LC_ALL, 'C')

__version__ = "5.1.0"
__flash_help__ = '''
<p>This setting is highly dependent on your device!<p>
<p>
  Details at <a style="color: #004CE5;"
        href="https://www.esp32.com/viewtopic.php?p=5523&sid=08ef44e13610ecf2a2a33bb173b0fd5c#p5523">http://bit.ly/2v5Rd32</a>
  and in the <a style="color: #004CE5;" href="https://github.com/espressif/esptool/#flash-modes">esptool
  documentation</a>
<ul>
  <li>Most ESP32 and ESP8266 ESP-12 use DIO.</li>
  <li>Most ESP8266 ESP-01/07 use QIO.</li>
  <li>ESP8285 requires DOUT.</li>
</ul>
</p>
'''
__auto_select__ = "Auto-select"
__auto_select_explanation__ = "(first port with Espressif device)"
__supported_baud_rates__ = [9600, 57600, 74880, 115200, 230400, 460800, 921600]

# ---------------------------------------------------------------------------


# See discussion at http://stackoverflow.com/q/41101897/131929
class RedirectText:
    def __init__(self, text_ctrl):
        self.__out = text_ctrl

    def write(self, string):
        if string.startswith("\r"):
            # carriage return -> remove last line i.e. reset position to start of last line
            current_value = self.__out.GetValue()
            last_newline = current_value.rfind("\n")
            new_value = current_value[:last_newline + 1]  # preserve \n
            new_value += string[1:]  # chop off leading \r
            wx.CallAfter(self.__out.SetValue, new_value)
        else:
            wx.CallAfter(self.__out.AppendText, string)

    # noinspection PyMethodMayBeStatic
    def flush(self):
        # noinspection PyStatementEffect
        None

    # esptool >=3 handles output differently of the output stream is not a TTY
    # noinspection PyMethodMayBeStatic
    def isatty(self):
        return True

# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
class FlashingThread(threading.Thread):
    def __init__(self, parent, config):
        threading.Thread.__init__(self)
        self.daemon = True
        self._parent = parent
        self._config = config

    def run(self):
        try:
            # Basic heuristic: If the firmware path includes ".ino.bin", assume ESP32/Arduino
            # TODO: Replace this with a proper UI element for chip selection
            is_esp32_arduino = self._config.firmware_path and ".ino.bin" in os.path.basename(self._config.firmware_path)

            command = []

            if not self._config.port.startswith(__auto_select__):
                command.append("--port")
                command.append(self._config.port)

            command.extend(["--baud", str(self._config.baud)])

            if is_esp32_arduino:
                chip = "esp32s2"  # Hardcoded based on user feedback for .ino.bin files
                flash_freq = "80m" # Hardcoded for now
                flash_size = "4MB" # Hardcoded for now
                app_offset = "0x10000" # Default offset for app binary
                app_path = self._config.firmware_path

                # Get optional paths/offsets from config (populated by UI)
                custom_bootloader_path = self._config.custom_bootloader_path
                custom_bootloader_offset = self._config.custom_bootloader_offset
                custom_partitions_path = self._config.custom_partitions_path
                custom_partitions_offset = self._config.custom_partitions_offset


                command.extend([f"--chip", chip])
                command.extend(["--before", "default_reset", "--after", "hard_reset"])
                command.extend(["write_flash", "-z"])
                command.extend(["--flash_mode", self._config.mode]) # Keep mode configurable
                command.extend([f"--flash_freq", flash_freq, f"--flash_size", flash_size])

                # Build the flashing address/path pairs
                flash_files = []
                # Add bootloader if path is provided and exists
                if custom_bootloader_path and os.path.exists(custom_bootloader_path):
                     # Basic validation for offset format (optional)
                     if not custom_bootloader_offset.startswith("0x"): custom_bootloader_offset = "0x1000" # Fallback
                     flash_files.extend([custom_bootloader_offset, custom_bootloader_path])
                elif custom_bootloader_path: # Path provided but file doesn't exist
                    print(f"Warning: Bootloader file specified but not found: {custom_bootloader_path}")


                # Add partitions if path is provided and exists
                if custom_partitions_path and os.path.exists(custom_partitions_path):
                     # Basic validation for offset format (optional)
                     if not custom_partitions_offset.startswith("0x"): custom_partitions_offset = "0x8000" # Fallback
                     flash_files.extend([custom_partitions_offset, custom_partitions_path])
                elif custom_partitions_path: # Path provided but file doesn't exist
                     print(f"Warning: Partitions file specified but not found: {custom_partitions_path}")


                # Always include the main application binary (check existence)
                if app_path and os.path.exists(app_path):
                    if not app_offset.startswith("0x"): app_offset = "0x10000" # Fallback
                    flash_files.extend([app_offset, app_path])
                else:
                     self._parent.report_error(f"Application firmware file not found: {app_path}")
                     return # Cannot proceed without app firmware


                if not flash_files:
                     self._parent.report_error("No valid firmware files specified.")
                     return


                command.extend(flash_files)

            else:
                # Assume ESP8266 or generic single file flash
                chip = "esp8266" # Assume ESP8266 if not ESP32/Arduino
                command.extend([f"--chip", chip])
                command.extend(["--before", "default_reset", "--after", "hard_reset"])
                command.extend(["write_flash"])
                # https://github.com/espressif/esptool/issues/599
                command.extend(["--flash_size", "detect"])
                command.extend(["--flash_mode", self._config.mode])
                 # Check existence for ESP8266 firmware too
                if self._config.firmware_path and os.path.exists(self._config.firmware_path):
                    command.extend(["0x00000", self._config.firmware_path])
                else:
                    self._parent.report_error(f"Firmware file not found: {self._config.firmware_path}")
                    return # Cannot proceed


            if self._config.erase_before_flash:
                # Erase flag might need reconsideration depending on final ESP32 logic
                # Only erase if flashing only app (or ESP8266)
                should_erase = False
                if not is_esp32_arduino:
                    should_erase = True
                elif is_esp32_arduino and len(flash_files) == 2: # ESP32 with only app file
                    should_erase = True

                if should_erase:
                     command.append("--erase-all")
                elif is_esp32_arduino: # ESP32 with multiple files
                    print("Note: Erase flag ignored for ESP32 multi-file flashing.")


            print("Command: esptool.py %s\n" % " ".join(command))

            esptool.main(command)

            # The last line printed by esptool is "Staying in bootloader." -> some indication that the process is
            # done is needed
            print("\nFirmware successfully flashed. Unplug/replug or reset device \nto switch back to normal boot "
                  "mode.")
        except SerialException as e:
            self._parent.report_error(e.strerror)
            # No need to re-raise, just report error
        except FileNotFoundError as e: # More specific error
             self._parent.report_error(f"File not found error: {e}")
        except Exception as e:
            # Catch other potential errors like invalid JSON in config or esptool errors
            self._parent.report_error(f"An unexpected error occurred: {str(e)}")
            # Consider logging the full traceback for debugging


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# DTO between GUI and flashing thread
class FlashConfig:
    def __init__(self):
        self.baud = 115200
        self.erase_before_flash = False
        self.mode = "dio"
        self.firmware_path = None
        self.port = None
        # ESP32 specific optional paths/offsets
        self.custom_bootloader_path = None
        self.custom_bootloader_offset = "0x1000"
        self.custom_partitions_path = None
        self.custom_partitions_offset = "0x8000"


    @classmethod
    def load(cls, file_path):
        conf = cls()
        if os.path.exists(file_path):
            try: # Added try-except for robustness if config file is old/malformed
                with open(file_path, 'r') as f:
                    data = json.load(f)
                conf.port = data.get('port') # Use .get for safer loading
                conf.baud = data.get('baud', 115200)
                conf.mode = data.get('mode', 'dio')
                conf.erase_before_flash = data.get('erase', False)
                # Load ESP32 specific fields if they exist
                conf.custom_bootloader_path = data.get('custom_bootloader_path')
                conf.custom_bootloader_offset = data.get('custom_bootloader_offset', "0x1000")
                conf.custom_partitions_path = data.get('custom_partitions_path')
                conf.custom_partitions_offset = data.get('custom_partitions_offset', "0x8000")
            except (json.JSONDecodeError, KeyError) as e:
                 print(f"Warning: Could not load config file {file_path}: {e}")
                 # Use default values
        return conf

    def safe(self, file_path):
        data = {
            'port': self.port,
            'baud': self.baud,
            'mode': self.mode,
            'erase': self.erase_before_flash,
            # Save ESP32 specific fields
            'custom_bootloader_path': self.custom_bootloader_path,
            'custom_bootloader_offset': self.custom_bootloader_offset,
            'custom_partitions_path': self.custom_partitions_path,
            'custom_partitions_offset': self.custom_partitions_offset,
        }
        try: # Added try-except for robustness
            with open(file_path, 'w') as f:
                json.dump(data, f, indent=4) # Added indent for readability
        except IOError as e:
             print(f"Warning: Could not save config file {file_path}: {e}")


    def is_complete(self):
        # Only firmware_path and port are strictly mandatory to start
        return self.firmware_path is not None and self.port is not None

# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
class NodeMcuFlasher(wx.Frame):

    def __init__(self, parent, title):
        wx.Frame.__init__(self, parent, -1, title, size=(725, 650),
                          style=wx.DEFAULT_FRAME_STYLE | wx.NO_FULL_REPAINT_ON_RESIZE)
        self._config = FlashConfig.load(self._get_config_file_path())

        self._build_status_bar()
        self._set_icons()
        self._build_menu_bar()
        self._init_ui()

        sys.stdout = RedirectText(self.console_ctrl)

        self.Centre(wx.BOTH)
        self.Show(True)
        print("Connect your device")
        print("\nIf you chose the serial port auto-select feature you might need to ")
        print("turn off Bluetooth")

    def _init_ui(self):
        def on_reload(event):
            self.choice.SetItems(self._get_serial_ports())

        def on_baud_changed(event):
            radio_button = event.GetEventObject()

            if radio_button.GetValue():
                self._config.baud = radio_button.rate

        def on_mode_changed(event):
            radio_button = event.GetEventObject()

            if radio_button.GetValue():
                self._config.mode = radio_button.mode

        def on_erase_changed(event):
            radio_button = event.GetEventObject()

            if radio_button.GetValue():
                self._config.erase_before_flash = radio_button.erase

        def on_clicked(event):
            self.console_ctrl.SetValue("")
            worker = FlashingThread(self, self._config)
            worker.start()

        def on_select_port(event):
            choice = event.GetEventObject()
            self._config.port = choice.GetString(choice.GetSelection())

        def on_pick_file(event):
            path = event.GetPath().replace("'", "")
            self._config.firmware_path = path
            # TODO (Optional): Add logic here to show/hide ESP32 options based on path?

        def on_pick_bootloader(event):
            path = event.GetPath().replace("'", "")
            self._config.custom_bootloader_path = path

        def on_bootloader_offset_changed(event):
            offset = event.GetString()
            # Basic validation, could be improved (e.g., regex for hex)
            if offset.startswith("0x"):
                self._config.custom_bootloader_offset = offset
            else:
                 # Optionally provide feedback to the user about invalid format
                 print(f"Invalid bootloader offset format: {offset}. Using default {self._config.custom_bootloader_offset}")
                 # Or reset the text control value: self.bootloader_offset_ctrl.SetValue(self._config.custom_bootloader_offset)


        def on_pick_partitions(event):
            path = event.GetPath().replace("'", "")
            self._config.custom_partitions_path = path

        def on_partitions_offset_changed(event):
             offset = event.GetString()
             if offset.startswith("0x"):
                 self._config.custom_partitions_offset = offset
             else:
                 print(f"Invalid partitions offset format: {offset}. Using default {self._config.custom_partitions_offset}")
                 # self.partitions_offset_ctrl.SetValue(self._config.custom_partitions_offset)

        # Fix popup that never goes away. Moved definition here before panel.Bind
        def onHover(event):
            global hovered
            if(len(hovered) != 0 ):
                hovered[0].Dismiss()
                hovered = []

        panel = wx.Panel(self)
        panel.Bind(wx.EVT_MOTION, onHover) # Now onHover is defined

        hbox = wx.BoxSizer(wx.HORIZONTAL)

        # Correct number of rows is 11 (Port, FW, BL, BL Offset, Part, Part Offset, Baud, Mode, Erase, Button, Console)
        fgs = wx.FlexGridSizer(11, 2, 10, 10) # Rows, Cols, vgap, hgap

        self.choice = wx.Choice(panel, choices=self._get_serial_ports())
        self.choice.Bind(wx.EVT_CHOICE, on_select_port)
        self._select_configured_port()

        reload_button = wx.Button(panel, label="Reload")
        reload_button.Bind(wx.EVT_BUTTON, on_reload)
        reload_button.SetToolTip("Reload serial device list")

        # Main Firmware File Picker
        file_picker = wx.FilePickerCtrl(panel, style=wx.FLP_USE_TEXTCTRL, message="Select Firmware File")
        file_picker.Bind(wx.EVT_FILEPICKER_CHANGED, on_pick_file)

        serial_boxsizer = wx.BoxSizer(wx.HORIZONTAL)
        serial_boxsizer.Add(self.choice, 1, wx.EXPAND)
        serial_boxsizer.Add(reload_button, flag=wx.LEFT, border=10)

        baud_boxsizer = wx.BoxSizer(wx.HORIZONTAL)

        def add_baud_radio_button(sizer, index, baud_rate):
            style = wx.RB_GROUP if index == 0 else 0
            radio_button = wx.RadioButton(panel, name="baud-%d" % baud_rate, label="%d" % baud_rate, style=style)
            radio_button.rate = baud_rate
            radio_button.SetValue(baud_rate == self._config.baud)
            radio_button.Bind(wx.EVT_RADIOBUTTON, on_baud_changed)
            sizer.Add(radio_button)
            sizer.AddSpacer(10)

        for idx, rate in enumerate(__supported_baud_rates__):
            add_baud_radio_button(baud_boxsizer, idx, rate)

        flashmode_boxsizer = wx.BoxSizer(wx.HORIZONTAL)

        def add_flash_mode_radio_button(sizer, index, mode, label):
            style = wx.RB_GROUP if index == 0 else 0
            radio_button = wx.RadioButton(panel, name="mode-%s" % mode, label="%s" % label, style=style)
            radio_button.Bind(wx.EVT_RADIOBUTTON, on_mode_changed)
            radio_button.mode = mode
            radio_button.SetValue(mode == self._config.mode)
            sizer.Add(radio_button)
            sizer.AddSpacer(10)

        add_flash_mode_radio_button(flashmode_boxsizer, 0, "qio", "Quad I/O (QIO)")
        add_flash_mode_radio_button(flashmode_boxsizer, 1, "dio", "Dual I/O (DIO)")
        add_flash_mode_radio_button(flashmode_boxsizer, 2, "dout", "Dual Output (DOUT)")

        erase_boxsizer = wx.BoxSizer(wx.HORIZONTAL)

        def add_erase_radio_button(sizer, index, erase_before_flash, label, value):
            style = wx.RB_GROUP if index == 0 else 0
            radio_button = wx.RadioButton(panel, name="erase-%s" % erase_before_flash, label="%s" % label, style=style)
            radio_button.Bind(wx.EVT_RADIOBUTTON, on_erase_changed)
            radio_button.erase = erase_before_flash
            radio_button.SetValue(value)
            sizer.Add(radio_button)
            sizer.AddSpacer(10)

        erase = self._config.erase_before_flash
        add_erase_radio_button(erase_boxsizer, 0, False, "no", erase is False)
        add_erase_radio_button(erase_boxsizer, 1, True, "yes, wipes all data", erase is True)

        # --- New ESP32 Controls ---
        bootloader_label = wx.StaticText(panel, label="Bootloader (optional)")
        self.bootloader_picker = wx.FilePickerCtrl(panel, style=wx.FLP_USE_TEXTCTRL | wx.FLP_FILE_MUST_EXIST, message="Select Bootloader Binary")
        self.bootloader_picker.Bind(wx.EVT_FILEPICKER_CHANGED, on_pick_bootloader)
        if self._config.custom_bootloader_path: # Restore path on load
             self.bootloader_picker.SetPath(self._config.custom_bootloader_path)


        bootloader_offset_label = wx.StaticText(panel, label="Bootloader offset")
        self.bootloader_offset_ctrl = wx.TextCtrl(panel, value=self._config.custom_bootloader_offset)
        self.bootloader_offset_ctrl.Bind(wx.EVT_TEXT, on_bootloader_offset_changed)
        self.bootloader_offset_ctrl.SetToolTip("Address (e.g., 0x1000)")

        partitions_label = wx.StaticText(panel, label="Partitions (optional)")
        self.partitions_picker = wx.FilePickerCtrl(panel, style=wx.FLP_USE_TEXTCTRL | wx.FLP_FILE_MUST_EXIST, message="Select Partitions Binary")
        self.partitions_picker.Bind(wx.EVT_FILEPICKER_CHANGED, on_pick_partitions)
        if self._config.custom_partitions_path: # Restore path on load
             self.partitions_picker.SetPath(self._config.custom_partitions_path)


        partitions_offset_label = wx.StaticText(panel, label="Partitions offset")
        self.partitions_offset_ctrl = wx.TextCtrl(panel, value=self._config.custom_partitions_offset)
        self.partitions_offset_ctrl.Bind(wx.EVT_TEXT, on_partitions_offset_changed)
        self.partitions_offset_ctrl.SetToolTip("Address (e.g., 0x8000)")


        # --- Flash Button and Console ---
        button = wx.Button(panel, -1, "Flash Device")
        button.Bind(wx.EVT_BUTTON, on_clicked)

        self.console_ctrl = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.console_ctrl.SetFont(wx.Font((0, 13), wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL,
                                          wx.FONTWEIGHT_NORMAL))
        self.console_ctrl.SetBackgroundColour(wx.WHITE)
        self.console_ctrl.SetForegroundColour(wx.BLUE)
        self.console_ctrl.SetDefaultStyle(wx.TextAttr(wx.BLUE))

        # --- Labels for Controls ---
        port_label = wx.StaticText(panel, label="Serial port")
        file_label = wx.StaticText(panel, label="Firmware")
        baud_label = wx.StaticText(panel, label="Baud rate")
        flashmode_label = wx.StaticText(panel, label="Flash mode")
        def on_info_hover(event):
            global hovered
            if(len(hovered) == 0):
                from HtmlPopupTransientWindow import HtmlPopupTransientWindow
                win = HtmlPopupTransientWindow(self, wx.SIMPLE_BORDER, __flash_help__, "#FFB6C1", (410, 140))

                image = event.GetEventObject()
                image_position = image.ClientToScreen((0, 0))
                image_size = image.GetSize()
                win.Position(image_position, (0, image_size[1]))

                win.Popup()
                hovered = [win]
        icon = wx.StaticBitmap(panel, wx.ID_ANY, images.Info.GetBitmap())
        icon.Bind(wx.EVT_MOTION, on_info_hover)
        flashmode_label_boxsizer = wx.BoxSizer(wx.HORIZONTAL)
        flashmode_label_boxsizer.Add(flashmode_label, 1, wx.EXPAND)
        flashmode_label_boxsizer.AddStretchSpacer(0)
        flashmode_label_boxsizer.Add(icon)
        erase_label = wx.StaticText(panel, label="Erase flash")
        console_label = wx.StaticText(panel, label="Console")

        # --- Add All Controls to Grid ---
        fgs.AddMany([
                    port_label, (serial_boxsizer, 1, wx.EXPAND),
                    file_label, (file_picker, 1, wx.EXPAND),
                    bootloader_label, (self.bootloader_picker, 1, wx.EXPAND),
                    bootloader_offset_label, (self.bootloader_offset_ctrl, 1, wx.EXPAND),
                    partitions_label, (self.partitions_picker, 1, wx.EXPAND),
                    partitions_offset_label, (self.partitions_offset_ctrl, 1, wx.EXPAND),
                    baud_label, baud_boxsizer,
                    flashmode_label_boxsizer, flashmode_boxsizer,
                    erase_label, erase_boxsizer,
                    (wx.StaticText(panel, label="")), (button, 1, wx.EXPAND), # Empty label for button alignment
                    console_label, (self.console_ctrl, 1, wx.EXPAND) # Console spans 1 label + 1 control = 2 cols
                   ])
        # The console is in the 11th row (index 10)
        fgs.AddGrowableRow(10, 1)
        fgs.AddGrowableCol(1, 1)
        hbox.Add(fgs, proportion=2, flag=wx.ALL | wx.EXPAND, border=15)
        panel.SetSizer(hbox)

    def _select_configured_port(self):
        count = 0
        for item in self.choice.GetItems():
            if item == self._config.port:
                self.choice.Select(count)
                break
            count += 1

    @staticmethod
    def _get_serial_ports():
        ports = [__auto_select__ + " " + __auto_select_explanation__]
        for port, desc, hwid in sorted(list_ports.comports()):
            ports.append(port)
        return ports

    def _set_icons(self):
        self.SetIcon(images.Icon.GetIcon())

    def _build_status_bar(self):
        self.statusBar = self.CreateStatusBar(2, wx.STB_SIZEGRIP)
        self.statusBar.SetStatusWidths([-2, -1])
        status_text = "Welcome to NodeMCU PyFlasher %s" % __version__
        self.statusBar.SetStatusText(status_text, 0)

    def _build_menu_bar(self):
        self.menuBar = wx.MenuBar()

        # File menu
        file_menu = wx.Menu()
        wx.App.SetMacExitMenuItemId(wx.ID_EXIT)
        exit_item = file_menu.Append(wx.ID_EXIT, "E&xit\tCtrl-Q", "Exit NodeMCU PyFlasher")
        exit_item.SetBitmap(images.Exit.GetBitmap())
        self.Bind(wx.EVT_MENU, self._on_exit_app, exit_item)
        self.menuBar.Append(file_menu, "&File")

        # Help menu
        help_menu = wx.Menu()
        help_item = help_menu.Append(wx.ID_ABOUT, '&About NodeMCU PyFlasher', 'About')
        self.Bind(wx.EVT_MENU, self._on_help_about, help_item)
        self.menuBar.Append(help_menu, '&Help')

        self.SetMenuBar(self.menuBar)

    @staticmethod
    def _get_config_file_path():
        return wx.StandardPaths.Get().GetUserConfigDir() + "/nodemcu-pyflasher.json"

    # Menu methods
    def _on_exit_app(self, event):
        self._config.safe(self._get_config_file_path()) # Ensure the updated safe method is called
        self.Close(True)

    def _on_help_about(self, event):
        from About import AboutDlg
        about = AboutDlg(self)
        about.ShowModal()
        about.Destroy()

    def report_error(self, message):
        self.console_ctrl.SetValue(message)

    def log_message(self, message):
        self.console_ctrl.AppendText(message)

# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
class MySplashScreen(wx.adv.SplashScreen):
    def __init__(self):
        global hovered
        hovered = []
        wx.adv.SplashScreen.__init__(self, images.Splash.GetBitmap(),
                                     wx.adv.SPLASH_CENTRE_ON_SCREEN | wx.adv.SPLASH_TIMEOUT, 2500, None, -1)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.__fc = wx.CallLater(2000, self._show_main)

    def _on_close(self, evt):
        # Make sure the default handler runs too so this window gets
        # destroyed
        evt.Skip()
        self.Hide()

        # if the timer is still running then go ahead and show the
        # main frame now
        if self.__fc.IsRunning():
            self.__fc.Stop()
            self._show_main()

    def _show_main(self):
        frame = NodeMcuFlasher(None, "NodeMCU PyFlasher")
        frame.Show()
        if self.__fc.IsRunning():
            self.Raise()

# ---------------------------------------------------------------------------


# ----------------------------------------------------------------------------
class App(wx.App, wx.lib.mixins.inspection.InspectionMixin):
    def OnInit(self):
        # see https://discuss.wxpython.org/t/wxpython4-1-1-python3-8-locale-wxassertionerror/35168
        self.ResetLocale()
        wx.SystemOptions.SetOption("mac.window-plain-transition", 1)
        self.SetAppName("NodeMCU PyFlasher")

        # Create and show the splash screen.  It will then create and
        # show the main frame when it is time to do so.  Normally when
        # using a SplashScreen you would create it, show it and then
        # continue on with the application's initialization, finally
        # creating and showing the main application window(s).  In
        # this case we have nothing else to do so we'll delay showing
        # the main frame until later (see ShowMain above) so the users
        # can see the SplashScreen effect.
        splash = MySplashScreen()
        splash.Show()

        return True


# ---------------------------------------------------------------------------
def main():
    app = App(False)
    app.MainLoop()
# ---------------------------------------------------------------------------


if __name__ == '__main__':
    __name__ = 'Main'
    main()

