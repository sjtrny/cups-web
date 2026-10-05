#!/bin/sh
set -eu

# Install Brother's pinned amd64-compatible i386 driver without its host
# service/queue installation scripts. Only this container uses the driver.
driver_deb=/tmp/ql1110nwbpdrv.deb
curl --fail --location --retry 3 --output "$driver_deb" \
  https://download.brother.com/welcome/dlfp100575/ql1110nwbpdrv-2.1.4-0.i386.deb
printf '%s  %s\n' \
  93670aa9b11a8a4090777ca61648d32d6bd6c087d2a1998a4b5ac93b0b6b4a0a \
  "$driver_deb" | sha256sum --check --status
dpkg-deb --extract "$driver_deb" /
rm "$driver_deb"

driver_dir=/opt/brother/PTouch/ql1110nwb
ln -s "$driver_dir/cupswrapper/brother_lpdwrapper_ql1110nwb" \
  /usr/lib/cups/filter/brother_lpdwrapper_ql1110nwb

# The physical roll is continuous, so the stock die-cut 102x152 preset is
# unsuitable. Use Brother's own tool to create a continuous 152 mm preset.
mkdir -p /etc/cups/ppd
temporary_ppd=/etc/cups/ppd/QL-1110NWB.ppd
cp "$driver_dir/cupswrapper/brother_ql1110nwb_printer_en.ppd" "$temporary_ppd"
brpapertoollpr_ql1110nwb -P QL-1110NWB -n Continuous102x152 -w 102 -h 152
custom_key=$(sed -n 's#^\(BrL_[^/]*\)/Continuous102x152:.*#\1#p' \
  "$driver_dir/inf/paperinfql1110nwb")
test -n "$custom_key"

# Keep this preset's identifier stable when rebuilding the image.
stable_key=BrL_102X1____116609810000
sed -i "s/$custom_key/$stable_key/g" "$temporary_ppd" \
  "$driver_dir/inf/paperinfql1110nwb" "$driver_dir/inf/ImagingArea" \
  "$driver_dir/inf/brql1110nwbfunc"

# Preserve the existing Windows queue's standard 4x6 media choice. Both
# explicit PageSize and PPD defaults must map to the continuous-roll preset.
wrapper="$driver_dir/cupswrapper/brother_lpdwrapper_ql1110nwb"
sed -i \
  -e "/^[[:space:]]*'PageSize=17x54'[[:space:]]*=>/i\  'PageSize=4x6' => {\"opt\"=>\"-media\", \"val\"=>\"Continuous102x152\"}," \
  -e "/^[[:space:]]*'17x54'[[:space:]]*=>/i\  '4x6' => {\"opt\"=>\"-media\", \"val\"=>\"Continuous102x152\"}," \
  "$wrapper"

awk -v key="$stable_key" '
  { print }
  index($0, key) && /^\*(PageSize|PageRegion|ImageableArea|PaperDimension) / {
    line = $0
    gsub(key, "4x6", line)
    gsub("Continuous102x152", "4x6 on 102mm continuous roll", line)
    if (line ~ /^\*PaperDimension /) {
      sub(/"[^"]*"$/, "\"288.00 432.00\"", line)
    }
    print line
  }
' "$temporary_ppd" > /usr/local/share/brother-label-printer.ppd
sed -i 's/^\*DefaultPageSize:.*/\*DefaultPageSize: 4x6/; s/^\*DefaultPageRegion:.*/\*DefaultPageRegion: 4x6/; s/^\*DefaultImageableArea:.*/\*DefaultImageableArea: 4x6/; s/^\*DefaultPaperDimension:.*/\*DefaultPaperDimension: 4x6/' \
  /usr/local/share/brother-label-printer.ppd
# The stock die-cut preset also has 4x6 inch dimensions. Remove it so an IPP
# job specified by dimensions resolves to the continuous-roll preset.
sed -i '/^\*PageSize 102x152\//d; /^\*PageRegion 102x152\//d; /^\*ImageableArea 102x152\//d; /^\*PaperDimension 102x152\//d; /^\*UIConstraints:.*PageSize 102x152/d' \
  /usr/local/share/brother-label-printer.ppd
rm "$temporary_ppd"
