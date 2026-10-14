// Exercise TCMB Code inside the image's real remote extension host before publishing.
// Requires Docker, zip, python3 and Playwright (with Chromium) on the Linux CI runner.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');

const image = process.argv[2];
assert(image, 'Usage: node smoke-vscode.cjs IMAGE');
const containerfile = fs.readFileSync(path.join(__dirname, '../../containers/vscode-tcmb/Containerfile'), 'utf8');
const expectedVersion = (containerfile.match(/^ARG TCMB_CODE_VERSION=(\S+)$/m) || [])[1];
assert(expectedVersion, 'TCMB_CODE_VERSION is missing from containers/vscode-tcmb/Containerfile');
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'devcloud-tcmb-code-smoke-'));
const container = `devcloud-tcmb-code-smoke-${process.pid}`;
const docker = (...args) => execFileSync('docker', args, { encoding: 'utf8', timeout: 30000, stdio: ['ignore', 'pipe', 'pipe'] });
const delay = (ms) => new Promise(resolve => setTimeout(resolve, ms));
const userDir = '/home/coder/.local/share/code-server/User';
const configDir = '/home/coder/.config/kilo';

fs.mkdirSync(path.join(fixture, 'extension'));
fs.writeFileSync(path.join(fixture, 'extension/package.json'), JSON.stringify({
  name: 'tcmb-code-smoke', publisher: 'devcloud-test', version: '0.0.1',
  engines: { vscode: '^1.105.0' }, main: './extension.js',
  activationEvents: ['onStartupFinished'],
}));
fs.writeFileSync(path.join(fixture, 'extension/extension.js'), `
const vscode = require('vscode');
const fs = require('fs');
exports.activate = async () => {
  try {
    const config = vscode.workspace.getConfiguration();
    if (config.get('chat.disableAIFeatures') !== true) throw Error('Native Chat is enabled');
    if (config.get('kilo-code.new.model.providerID') !== 'litellm') throw Error('Managed provider is not selected');
    if (config.get('kilo-code.new.model.modelID') !== 'ci-dummy-model') throw Error('Managed model is not selected');
    const tcmb = vscode.extensions.getExtension('tcmb.tcmbcode');
    if (!tcmb) throw Error('TCMB Code is missing from the extension registry');
    if (tcmb.packageJSON.version !== ${JSON.stringify(expectedVersion)}) throw Error('Unexpected TCMB Code version ' + tcmb.packageJSON.version);
    await tcmb.activate();
    if (!tcmb.isActive) throw Error('TCMB Code did not activate');
    await vscode.commands.executeCommand('kilo-code.SidebarProvider.focus');
    fs.writeFileSync('/tmp/devcloud-tcmb-code-smoke.json', JSON.stringify({ok:true}));
  } catch (error) {
    fs.writeFileSync('/tmp/devcloud-tcmb-code-smoke.json', JSON.stringify({ok:false,error:String(error.stack || error)}));
  }
};
`);
fs.writeFileSync(path.join(fixture, '[Content_Types].xml'),
  '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="json" ContentType="application/json"/><Default Extension="js" ContentType="application/javascript"/><Default Extension="vsixmanifest" ContentType="text/xml"/></Types>');
fs.writeFileSync(path.join(fixture, 'extension.vsixmanifest'),
  '<?xml version="1.0"?><PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011"><Metadata><Identity Language="en-US" Id="tcmb-code-smoke" Version="0.0.1" Publisher="devcloud-test"/><DisplayName>TCMB Code smoke test</DisplayName><Description xml:space="preserve">CI only</Description><Properties><Property Id="Microsoft.VisualStudio.Code.Engine" Value="^1.105.0"/></Properties></Metadata><Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation><Dependencies/><Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets></PackageManifest>');
execFileSync('zip', ['-qr', 'smoke.vsix', 'extension', '[Content_Types].xml', 'extension.vsixmanifest'], { cwd: fixture });

// The same renderer the controller uses, with dummy keys and an unreachable gateway.
const managed = JSON.parse(execFileSync('python3', ['-c', `
import json
from app.tcmb_code import managed_vscode_settings, render_config
config = render_config(
    gateway_url="http://127.0.0.1:9",
    models=[{"model_id": "ci-dummy-model", "name": "CI model"}],
    default_model="ci-dummy-model",
    user_key="ci-dummy-user-key",
    library_url="http://127.0.0.1:9",
    indexing_enabled=True,
    indexing_key="ci-dummy-indexing-key",
)
print(json.dumps({"config": config, "settings": json.loads(managed_vscode_settings(config))}))
`], { encoding: 'utf8' }));
fs.writeFileSync(path.join(fixture, 'tcmb.jsonc'), managed.config);
// This is a generated, empty CI project. Trust it explicitly for the test;
// otherwise Restricted Mode prevents both TCMB Code and this fixture activating.
// Production images retain the user's normal workspace-trust controls.
fs.writeFileSync(path.join(fixture, 'settings.json'), JSON.stringify({
  ...managed.settings, 'security.workspace.trust.enabled': false,
}));

(async () => {
  let browser;
  let page;
  try {
    docker('create', '--name', container, '-p', '127.0.0.1::8080', image);
    docker('cp', path.join(fixture, 'smoke.vsix'), `${container}:/tmp/smoke.vsix`);
    docker('start', container);
    docker('exec', container, 'install', '-d', '-m', '700', configDir);
    docker('cp', path.join(fixture, 'tcmb.jsonc'), `${container}:${configDir}/tcmb.jsonc`);
    docker('cp', path.join(fixture, 'settings.json'), `${container}:${userDir}/settings.json`);
    docker('exec', '--user', 'root', container, 'chown', '-R', 'coder:coder', '/home/coder/.config', `${userDir}/settings.json`);
    console.log(docker('exec', container, 'code-server', '--install-extension', '/tmp/smoke.vsix'));
    console.log(docker('exec', container, 'code-server', '--list-extensions', '--show-versions'));
    // Image-level checks: the Linux agent CLI runs and LanceDB's native binding loads.
    console.log(docker('exec', container, 'sh', '-c',
      'cli="$(ls -d /home/coder/.local/share/code-server/extensions/tcmb.tcmbcode-*/bin/kilo)" && "$cli" --version'));
    docker('exec', container, 'sh', '-c', 'test -f "$KILO_LANCEDB_PATH" && node -e "require(process.env.KILO_LANCEDB_PATH)"');
    docker('exec', container, 'sh', '-c', 'command -v rg && command -v uv && command -v node && command -v python3');
    const port = docker('port', container, '8080/tcp').trim().split(':').pop();
    browser = await chromium.launch({ headless: true });
    page = await browser.newPage();
    page.on('pageerror', error => console.error('Browser error:', String(error)));
    page.on('console', message => { if (message.type() === 'error') console.error('Browser console:', message.text()); });
    await page.goto(`http://127.0.0.1:${port}/?folder=/home/coder/project`, { waitUntil: 'domcontentloaded', timeout: 60000 });
    const deadline = Date.now() + 120000;
    let activated = false;
    let inputVisible = false;
    let agentRunning = false;
    while (Date.now() < deadline) {
      try {
        const result = JSON.parse(docker('exec', container, 'cat', '/tmp/devcloud-tcmb-code-smoke.json'));
        assert(result.ok, result.error);
        activated = true;
      } catch (error) {
        if (error instanceof assert.AssertionError) throw error;
      }
      if (activated && !inputVisible) {
        for (const frame of page.frames()) {
          if (!frame.url().includes('webview')) continue;
          inputVisible = await frame.locator('textarea, [contenteditable="true"], [role="textbox"]').first().isVisible().catch(() => false);
          if (inputVisible) break;
        }
      }
      if (activated && !agentRunning) {
        try {
          docker('exec', container, 'sh', '-c', 'pgrep -f "tcmb.tcmbcode-.*/bin/kilo" >/dev/null');
          agentRunning = true;
        } catch {}
      }
      if (activated && inputVisible && agentRunning) break;
      await delay(1000);
    }
    assert(activated, 'TCMB Code activation timed out in the remote extension host');
    assert(agentRunning, 'TCMB Code activated but its agent CLI did not start');
    assert(inputVisible, 'TCMB Code activated but its chat input did not render');
    console.log(`PASS: TCMB Code ${expectedVersion} activates, starts its agent, renders chat; LanceDB loads; native Chat is disabled.`);
  } catch (error) {
    if (page) {
      console.error('Workbench:', (await page.locator('body').innerText().catch(() => '')).slice(-12000));
      await page.screenshot({ path: path.join(process.env.RUNNER_TEMP || os.tmpdir(), 'tcmb-code-smoke.png') }).catch(() => {});
    }
    try { console.error(docker('logs', container)); } catch {}
    try { console.error(docker('exec', container, 'sh', '-c', 'find /home/coder/.local/share/code-server/logs -name "*exthost*.log" -exec tail -n 120 {} +')); } catch {}
    try { console.error(docker('exec', container, 'sh', '-c', 'find /home/coder/.local/share/code-server/logs -name "remoteagent.log" -exec tail -n 80 {} +')); } catch {}
    try { console.error(docker('exec', container, 'sh', '-c', 'find /home/coder/.local/share/kilo -name "*.log" -exec tail -n 80 {} + 2>/dev/null')); } catch {}
    throw error;
  } finally {
    if (browser) await browser.close();
    try { docker('rm', '-f', container); } catch {}
    fs.rmSync(fixture, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
