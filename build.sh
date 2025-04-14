#!/usr/bin/env bash
# rm -fr build dist
VERSION=5.1.0
NAME="NodeMCU PyFlasher"
DIST_NAME="NodeMCU-PyFlasher"

pyinstaller --log-level=DEBUG \
            --noconfirm \
            build-on-mac.spec

# https://github.com/sindresorhus/create-dmg
# create-dmg "dist/$NAME.app"
# The line above implicitly created "$NAME $VERSION.dmg" in the root.
# Let's explicitly create it in the dist dir.
create-dmg "dist/$DIST_NAME.dmg" "dist/$NAME.app"

# mv "$NAME $VERSION.dmg" "dist/$DIST_NAME.dmg"
