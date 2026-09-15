// Exercise Cline inside the image's real remote extension host before publishing.
// Requires Docker, zip, and Playwright (with Chromium) on the Linux CI runner.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');

const image = process.argv[2];
assert(image, 'Usage: node smoke-vscode.cjs IMAGE');
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'devcloud-cline-smoke-'));
const container = `devcloud-cline-smoke-${process.pid}`;
const docker = (...args) => execFileSync('docker', args, { encoding: 'utf8', timeout: 30000 });
const delay = (ms) => new Promise(resolve => setTimeout(resolve, ms));

fs.mkdirSync(path.join(fixture, 'extension'));
fs.writeFileSync(path.join(fixture, 'extension/package.json'), JSON.stringify({
  name: 'cline-smoke', publisher: 'devcloud-test', version: '0.0.1',
  engines: { vscode: '^1.101.0' }, main: './extension.js',
  activationEvents: ['onStartupFinished'],
}));
fs.writeFileSync(path.join(fixture, 'extension/extension.js'), `
const vscode = require('vscode');
const fs = require('fs');
exports.activate = async () => {
  try {
    const config = vscode.workspace.getConfiguration();
    if (config.get('chat.disableAIFeatures') !== true) throw Error('Native Chat is enabled');
    if (config.get('cline.rollout.bundleOverride') !== 'legacy') throw Error('Cline bundle is not locked');
    const cline = vscode.extensions.getExtension('saoudrizwan.claude-dev');
    if (!cline) throw Error('Cline is missing from the extension registry');
    if (cline.packageJSON.version !== '4.1.17') throw Error('Unexpected Cline version');
    await cline.activate();
    if (!cline.isActive) throw Error('Cline did not activate');
    await vscode.commands.executeCommand('claude-dev.SidebarProvider.focus');
    fs.writeFileSync('/tmp/devcloud-cline-smoke.json', JSON.stringify({ok:true}));
  } catch (error) {
    fs.writeFileSync('/tmp/devcloud-cline-smoke.json', JSON.stringify({ok:false,error:String(error.stack || error)}));
  }
};
`);
fs.writeFileSync(path.join(fixture, '[Content_Types].xml'),
  '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="json" ContentType="application/json"/><Default Extension="js" ContentType="application/javascript"/><Default Extension="vsixmanifest" ContentType="text/xml"/></Types>');
fs.writeFileSync(path.join(fixture, 'extension.vsixmanifest'),
  '<?xml version="1.0"?><PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011"><Metadata><Identity Language="en-US" Id="cline-smoke" Version="0.0.1" Publisher="devcloud-test"/><DisplayName>Cline smoke test</DisplayName><Description xml:space="preserve">CI only</Description><Properties><Property Id="Microsoft.VisualStudio.Code.Engine" Value="^1.101.0"/></Properties></Metadata><Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation><Dependencies/><Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets></PackageManifest>');
execFileSync('zip', ['-qr', 'smoke.vsix', 'extension', '[Content_Types].xml', 'extension.vsixmanifest'], { cwd: fixture });
const managedFiles = JSON.parse(execFileSync('python3', ['-c',
  'import json; from app.cline import managed_cline_files; print(json.dumps(managed_cline_files("http://127.0.0.1:9", "ci-dummy-key", "ci-dummy-model")))'
], { encoding: 'utf8' }));
for (const [name, content] of Object.entries(managedFiles)) {
  const target = path.join(fixture, 'cline-data', name);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content);
}

(async () => {
  let browser;
  try {
    docker('create', '--name', container, '-p', '127.0.0.1::8080',
      '-e', 'CLINE_DATA_DIR=/tmp/cline-smoke-data', image);
    docker('cp', path.join(fixture, 'smoke.vsix'), `${container}:/tmp/smoke.vsix`);
    docker('cp', path.join(fixture, 'cline-data'), `${container}:/tmp/cline-smoke-data`);
    docker('start', container);
    docker('exec', '--user', 'root', container, 'chown', '-R', 'coder:coder', '/tmp/cline-smoke-data');
    docker('exec', container, 'code-server', '--install-extension', '/tmp/smoke.vsix');
    const port = docker('port', container, '8080/tcp').trim().split(':').pop();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${port}/?folder=/home/coder/project`, { waitUntil: 'domcontentloaded', timeout: 60000 });
    const trustButton = page.getByRole('button', { name: /Yes, I trust the authors/ });
    await trustButton.click({ timeout: 10000 }).catch(() => {});
    const deadline = Date.now() + 90000;
    let activated = false;
    let inputVisible = false;
    while (Date.now() < deadline) {
      try {
        const result = JSON.parse(docker('exec', container, 'cat', '/tmp/devcloud-cline-smoke.json'));
        assert(result.ok, result.error);
        activated = true;
      } catch (error) {
        if (error instanceof assert.AssertionError) throw error;
      }
      if (activated) {
        for (const frame of page.frames()) {
          if (!frame.url().includes('webview')) continue;
          inputVisible = await frame.locator('textarea, [contenteditable="true"], [role="textbox"]').first().isVisible().catch(() => false);
          if (inputVisible) break;
        }
      }
      if (activated && inputVisible) break;
      await delay(1000);
    }
    assert(activated, 'Cline activation timed out in the remote extension host');
    assert(inputVisible, 'Cline activated but its chat input did not render');
    // Also catch the previous stale-registration bug when the Admin toggle is off.
    docker('exec', container, 'code-server', '--uninstall-extension', 'saoudrizwan.claude-dev');
    assert(!docker('exec', container, 'code-server', '--list-extensions').toLowerCase().includes('saoudrizwan.claude-dev'));
    console.log('PASS: pinned Cline activates, renders chat, and uninstalls cleanly; native Chat is disabled.');
  } catch (error) {
    try { console.error(docker('logs', container)); } catch {}
    try { console.error(docker('exec', container, 'sh', '-c', 'find /home/coder/.local/share/code-server/logs -name "*exthost*.log" -exec tail -n 80 {} +')); } catch {}
    throw error;
  } finally {
    if (browser) await browser.close();
    try { docker('rm', '-f', container); } catch {}
    fs.rmSync(fixture, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
