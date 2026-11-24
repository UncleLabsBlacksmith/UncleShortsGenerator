# Third-party components

The MIT license in this repository applies to the application code. Bundled dependencies retain their own licenses and copyrights.

- Python and Tcl/Tk: runtime license files are preserved in the installer runtime. https://www.python.org/psf/license/
- Google API/auth libraries, requests, cryptography, tzdata and their dependencies: installed package metadata and license files are preserved under `runtime/Lib/site-packages`.
- FFmpeg and ffprobe are separate executable tools. The builder saves the exact binary's `-L` output in `licenses/FFmpeg.txt`. This release uses Gyan's FFmpeg 8.1.1 full build, whose license is stated by that output. FFmpeg is not relicensed under MIT. https://ffmpeg.org/legal.html and https://www.gyan.dev/ffmpeg/builds/

For redistributed FFmpeg builds, obtain corresponding source and build information from the provider for the exact build being distributed. Source releases: https://ffmpeg.org/releases/ ; Gyan build scripts: https://github.com/GyanD/codexffmpeg . Rebuilding with a different provider requires checking that provider's notices and source distribution requirements.
