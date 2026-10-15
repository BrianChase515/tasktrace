#!/bin/bash
# Sign TaskTrace.app with the project's long-lived certificate.
#
# macOS remembers permissions (Accessibility, Input Monitoring, Screen Recording) by the app's
# signature. Signing every build with the same certificate means updates keep their permissions.
#
# The certificate comes from the TASKTRACE_SIGNING_P12 secret (base64 .p12). Without it the build
# is ad-hoc signed: it still works, but permissions reset on every update.
set -eo pipefail

APP="$1"
P12_PASSWORD="tasktrace"          # protects the file in transit only; the secret itself is what's private
IDENTITY="TaskTrace Signing"
KEYCHAIN="${RUNNER_TEMP:-/tmp}/tasktrace-signing-$(uuidgen).keychain-db"
KEYCHAIN_PASSWORD="$(uuidgen)"
OPENSSL=/usr/bin/openssl            # macOS LibreSSL: reads and writes the .p12 format `security` expects
WORK="$(mktemp -d)"

if [ -z "$TASKTRACE_SIGNING_P12" ]; then
  echo "::warning::No TASKTRACE_SIGNING_P12 secret: ad-hoc signing. Permissions will reset on every update."
  codesign --force --deep --sign - "$APP"
  echo "adhoc" > signing-mode.txt
  rm -rf "$WORK"
  exit 0
fi

echo "Signing with the TaskTrace certificate (stable: permissions carry over between updates)"
echo "$TASKTRACE_SIGNING_P12" | base64 --decode > "$WORK/cert.p12"
echo "stable" > signing-mode.txt

"$OPENSSL" pkcs12 -in "$WORK/cert.p12" -passin "pass:$P12_PASSWORD" -nokeys -clcerts -out "$WORK/cert.pem"

security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
security set-keychain-settings -lut 3600 "$KEYCHAIN"
security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
security list-keychains -d user -s "$KEYCHAIN" $(security list-keychains -d user | tr -d '"')
security import "$WORK/cert.p12" -k "$KEYCHAIN" -P "$P12_PASSWORD" -T /usr/bin/codesign
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN" > /dev/null
# A self-signed certificate has to be trusted for code signing before codesign will use it.
sudo security add-trusted-cert -d -r trustRoot -p codeSign -k /Library/Keychains/System.keychain "$WORK/cert.pem"
security find-identity -v -p codesigning "$KEYCHAIN"
# Sign by exact fingerprint, so another certificate with the same name can never be picked by mistake.
FINGERPRINT="$(security find-identity -v -p codesigning "$KEYCHAIN" | awk '/"'"$IDENTITY"'"/ {print $2; exit}')"
[ -n "$FINGERPRINT" ] || { echo "No signing identity found in $KEYCHAIN"; exit 1; }
echo "Signing as $FINGERPRINT"

codesign --force --deep --timestamp=none --keychain "$KEYCHAIN" --sign "$FINGERPRINT" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
echo "Designated requirement (what macOS checks to keep permissions):"
codesign -d -r- "$APP" 2>&1 | grep designated

security delete-keychain "$KEYCHAIN" || true
rm -rf "$WORK"
