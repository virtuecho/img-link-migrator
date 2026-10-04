"""Check saved credentials across processes using temporary Keychain accounts."""
import pathlib
import subprocess
import tempfile
import uuid

root = pathlib.Path(__file__).resolve().parents[1]
app = (root / "app/MigratorApp.swift").read_text()
credentials = app[app.index("enum APICredentials {"):app.index("struct ImageRow:")]
with tempfile.TemporaryDirectory(prefix="migrator-keychain-check-") as folder:
    source = pathlib.Path(folder) / "check.swift"
    binary = pathlib.Path(folder) / "check"
    source.write_text("import Foundation\nimport Security\n" + credentials + '''
let account = CommandLine.arguments[2]
switch CommandLine.arguments[1] {
case "write":
    try APICredentials.save("temporary-check-value", provider: account)
    try APICredentials.save("updated-check-value", provider: account)
case "read":
    let saved = try APICredentials.load(account)
    precondition(saved == "updated-check-value")
case "delete":
    try APICredentials.save("", provider: account)
    let saved = try APICredentials.load(account)
    precondition(saved == "")
default: fatalError("Unknown check action")
}
''')
    subprocess.run(["xcrun", "swiftc", str(source), "-o", str(binary), "-framework", "Security"], check=True)
    account = "verification-" + uuid.uuid4().hex
    try:
        for action in ("write", "read"):
            subprocess.run([str(binary), action, account], check=True, timeout=15)
    finally:
        subprocess.run([str(binary), "delete", account], check=True, timeout=15)
    print("Keychain save, update, restore across processes, and removal passed.")
