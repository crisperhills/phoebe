phoebe is an adaptive, transcoding media robot for ICHC.

### Architecture ###

* Asynchronous event handling with [Circuits](http://circuitsframework.com/)
* Media URL retrieval with [youtube-dl](https://rg3.github.io/youtube-dl/)
* Media transcoding and transmission with [Gstreamer](https://lazka.github.io/pgi-docs/), bound to Python with [PyGObject](https://wiki.gnome.org/action/show/Projects/PyGObject) and [GObject introspection](https://wiki.gnome.org/Projects/GObjectIntrospection)

### Dependencies ###
+ Python 2.7+, with modules:
    - Circuits
    - lxml
    - OpenSSL
    - requests
    - yaml
+ Gstreamer 1.4.5+, compiled with support for GObject introspection, with non-base elements:
    - flvmux
    - h264parse
    - imagefreeze
    - lamemp3enc
    - mpegaudioparse
    - multifilesrc
    - pngdec
    - rtmpsink
    - souphttpsrc
    - x264enc
+ youtube-dl, kept up to date

### Source hierarchy ###
* **bin/** -- binaries
    * **play.py** -- phoebe-player runtime
* **filters/** -- keyword search filter modules (see `filters/filter.py.example`) 
* **lib/** -- application modules (a.k.a., "the good stuff")
    * **commands.py** -- command parsing and handlers
    * **core.py** -- ICHC API handler, message processing, player supervision, core event handlers
    * **events.py** -- Circuits Event classes for all generated events
    * **player.py** -- Gst-based player module
    * **utils.py** -- play-request container class, site filter methods
* **config.yaml** -- example main configuration file
* **permissions.yaml** -- example permissions configuration file
* **run.py** -- main runtime

### ICHC Account Requirements ###

* phoebe connects using the ICHC API and the ICHC RTMP endpoint, both features available only to supporters. Thus, the account you configure the bot to use must be a supporter account.
* You'll likely encounter unexpected behavior trying to log in to the same channel with the same account the bot is connected with, and messages will appear in random places and the overall function of the bot is likely to break down. Thus, a dedicated account is necessary.

### Basic installation instructions ###

1. Ensure **Python 2.7+** is installed and updated.
2. Compile and install **gstreamer** for your platform, along with the _base_, _good_, _bad_, and _ugly_ plugin packages, taking care to observe the following requirements when running `./configure`:
    * Compile all packages with support for **GObject introspection**, and **GLib asserts**.
    * Optional: Ensure **orc** is installed for orc optimization support (modest performance bump).
3. Compile and install the gstreamer **libva** plugins at the same version as those gsteamer packages previously installed.
4. Install **youtube-dl**; take note of where the executable is placed (e.g., `which youtube-dl`)
5. Create a directory for phoebe to run, then use `git checkout` to checkout the release branch into that directory.
6. Copy `config.yaml` and `permissions.yaml` and modify to fit your installation.
7. Ensure all files in the directory are owned by the same user that will run the application.
8. Run phoebe with `./run.py`