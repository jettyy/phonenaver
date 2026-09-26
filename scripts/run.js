// npm install / npm start 가 부르는 파일. 맥·윈도우에서 파이썬을 찾아 launcher.py 를 실행한다.
const { spawnSync } = require('node:child_process');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const mode = process.argv[2] || 'start';
const isWin = process.platform === 'win32';

const candidates = isWin
  ? [['py', '-3'], ['python'], ['python3']]
  : [['python3.13'], ['python3.12'], ['python3.11'], ['python3.10'], ['python3'], ['python']];

function works(cmd) {
  const r = spawnSync(cmd[0], [...cmd.slice(1), '-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'], {
    stdio: 'ignore',
    shell: false,
  });
  return r.status === 0;
}

const python = candidates.find(works);
if (!python) {
  console.error('\n❌ 파이썬 3.9 이상이 필요합니다.');
  if (isWin) {
    console.error('   https://www.python.org/downloads/ 에서 설치하세요. 설치 화면에서 "Add python.exe to PATH" 를 꼭 체크!');
  } else {
    console.error('   터미널에서 xcode-select --install 을 실행하거나 https://www.python.org/downloads/ 에서 설치하세요.');
  }
  console.error('   설치 후 창을 새로 열고 다시 실행하세요.\n');
  process.exit(1);
}

const env = { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' };
const r = spawnSync(python[0], [...python.slice(1), path.join(root, 'launcher.py'), mode], {
  cwd: root,
  stdio: 'inherit',
  env,
});
process.exit(r.status ?? 1);
