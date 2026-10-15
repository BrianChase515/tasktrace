#!/bin/bash
# Sign TaskTrace.app with the project's long-lived certificate.
#
# macOS remembers permissions (Accessibility, Input Monitoring, Screen Recording) by the app's
# signature. Signing every build with the same certificate means updates keep their permissions.
#
# The certificate comes from the TASKTRACE_SIGNING_P12 secret (base64 .p12). Without it, a
# throwaway certificate is made so the build still works, but permissions reset on every update.
set -eo pipefail

APP="$1"
P12_PASSWORD="tasktrace"          # protects the file in transit only; the secret itself is what's private
IDENTITY="TaskTrace Signing"
KEYCHAIN="$RUNNER_TEMP/tasktrace-signing.keychain-db"
KEYCHAIN_PASSWORD="$(uuidgen)"
OPENSSL=/usr/bin/openssl            # macOS LibreSSL: reads and writes the .p12 format `security` expects
WORK="$(mktemp -d)"

if [ -n "$TASKTRACE_SIGNING_P12" ]; then
  echo "Signing with the TaskTrace certificate (stable: permissions carry over between updates)"
  echo "$TASKTRACE_SIGNING_P12" | base64 --decode > "$WORK/cert.p12"
  echo "stable" > signing-mode.txt
else
  echo "::warning::No TASKTRACE_SIGNING_P12 secret. Using a throwaway certificate; permissions will reset on every update."
  "$OPENSSL" req -x509 -newkey rsa:2048 -sha256 -days 30 -nodes -keyout "$WORK/key.pem" -out "$WORK/cert.pem" \
    -subj "/CN=$IDENTITY" -extensions v3 -config <(printf '[req]\ndistinguished_name=dn\n[dn]\n[v3]\nbasicConstraints=critical,CA:false\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=critical,codeSigning\n')
  "$OPENSSL" pkcs12 -export -inkey "$WORK/key.pem" -in "$WORK/cert.pem" -name "$IDENTITY" \
    -passout "pass:$P12_PASSWORD" -out "$WORK/cert.p12"
  echo "throwaway" > signing-mode.txt
fi

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

codesign --force --deep --timestamp=none --keychain "$KEYCHAIN" --sign "$IDENTITY" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
echo "Designated requirement (what macOS checks to keep permissions):"
codesign -d -r- "$APP" 2>&1 | grep designated

rm -rf "$WORK"
