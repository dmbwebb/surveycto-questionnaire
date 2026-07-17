#!/bin/bash
# Recalculate Excel formulas by opening the file in Excel, saving, and closing.
# Usage: recalc_excel.sh <path_to_xlsx>

FILE="$1"

if [ -z "$FILE" ]; then
    echo "Usage: recalc_excel.sh <path_to_xlsx>"
    exit 1
fi

# Get absolute path
ABSPATH="$(cd "$(dirname "$FILE")" && pwd)/$(basename "$FILE")"

# Excel steals keyboard focus while open; concurrent typing lands in the sheet
# and corrupts it. Warn the user audibly before grabbing focus.
command -v say >/dev/null && say "Opening Excel briefly to recalculate a form. Please pause typing." 2>/dev/null

osascript -e "
set filePath to POSIX file \"$ABSPATH\"
tell application \"Microsoft Excel\"
    activate
    open filePath
    delay 3
    save active workbook
    delay 1
    close active workbook saving no
end tell
"
