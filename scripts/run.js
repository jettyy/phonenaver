// npm install / npm start 가 부르는 파일. 맥·윈도우에서 파이썬을 찾아 launcher.py 를 실행한다.
const { spawnSync } = require('node:child_process');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const mode = process.argv[2] || 'start';
const isWin = process.platform === 'win32';

const fs = require('node:fs');

// PATH 에서 찾을 후보
const onPath = isWin
  ? [['py', '-3'], ['python'], ['python3']]
  : [['python3.13'], ['python3.12'], ['python3.11'], ['python3.10'], ['python3'], ['python']];

// 윈도우: PATH 에 등록이 안 됐어도 설치 폴더에서 직접 찾는다 (새 버전부터)
function windowsInstalls() {
  const found = [];
  const bases = [
    path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python'),
    process.env.ProgramFiles || 'C:\\Program Files',
    process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)',
    'C:\\',
  ];
  for (const base of bases) {
    let names = [];
    try { names = fs.readdirSync(base); } catch { continue; }
    names
      .filter((n) => /^Python3\d+$/i.test(n))
      .sort((a, b) => Number(b.slice(7)) - Number(a.slice(7)))
      .forEach((n) => {
        const exe = path.join(base, n, 'python.exe');
        if (fs.existsSync(exe)) found.push([exe]);
      });
  }
  for (const launcher of [
    path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Launcher', 'py.exe'),
    path.join(process.env.SystemRoot || 'C:\\Windows', 'py.exe'),
  ]) {
    if (fs.existsSync(launcher)) found.push([launcher, '-3']);
  }
  return found;
}

function works(cmd) {
  const r = spawnSync(cmd[0], [...cmd.slice(1), '-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'], {
    stdio: 'ignore',
    shell: false,
  });
  return r.status === 0;
}

function findPython() {
  return [...onPath, ...(isWin ? windowsInstalls() : [])].find(works);
}

let python = findPython();

if (!python && isWin) {
  // 윈도우: 파이썬이 없으면 winget 으로 자동 설치
  console.log('\n🐍 파이썬이 없어서 자동으로 설치합니다. (몇 분 걸릴 수 있어요, 창이 뜨면 허용해 주세요)\n');
  spawnSync('winget', ['install', '-e', '--id', 'Python.Python.3.12', '--scope', 'user',
    '--accept-package-agreements', '--accept-source-agreements'], { stdio: 'inherit' });
  python = findPython();
}

if (!python) {
  console.error('\n❌ 파이썬 3.9 이상을 찾지 못했습니다.');
  if (isWin) {
    console.error('   https://www.python.org/downloads/ 에서 받아 설치하세요. 설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크!');
  } else {
    console.error('   터미널에서 xcode-select --install 을 실행하거나 https://www.python.org/downloads/ 에서 설치하세요.');
  }
  console.error('   설치 후 창을 새로 열고 다시 실행하세요.\n');
  process.exit(1);
}
console.log(`🐍 파이썬: ${python.join(' ')}`);

const env = { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' };
const r = spawnSync(python[0], [...python.slice(1), path.join(root, 'launcher.py'), mode], {
  cwd: root,
  stdio: 'inherit',
  env,
});
process.exit(r.status ?? 1);
