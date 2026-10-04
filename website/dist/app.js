const examples = {
  plan: { command: 'uv run mclaude --plan "分析项目，制定重构计划"', content: '<p class="terminal-label">MCLAUDE / PLAN MODE</p><p class="dim">✓ 读取项目目录与规则文件<br>✓ 搜索模块引用与调用关系<br>✓ 整理需要关注的代码路径</p><p>可以分三步推进：<br><br>  01 &nbsp;梳理当前模块的职责边界<br>  02 &nbsp;提取重复逻辑，保留现有接口<br>  03 &nbsp;运行相关测试，检查改动</p><div class="result">只读规划完成。等你决定下一步。 <span class="cursor"></span></div>' },
  edit: { command: 'uv run mclaude "提取重复逻辑，保持现有行为"', content: '<p class="terminal-label">MCLAUDE / PERMISSION REQUIRED</p><p class="dim">✓ 读取相关实现<br>✓ 定位需要替换的代码段</p><p>拟执行操作：replace_text<br><br>  文件 &nbsp;src/example.py（示例）<br>  变更 &nbsp;将重复逻辑提取为辅助函数</p><div class="result">写入前请求你的确认。<br>你可以允许本次操作，也可以拒绝。 <span class="cursor"></span></div>' },
  resume: { command: 'uv run mclaude --continue', content: '<p class="terminal-label">MCLAUDE / RESUME SESSION</p><p class="dim">✓ 载入最近会话<br>✓ 恢复消息历史</p><p>接着上一次的思路，继续推进任务。<br><br>会话恢复会重建消息历史，<br>不会重放历史工具调用。</p><div class="result">准备好了。下一步想做什么？ <span class="cursor"></span></div>' }
};
const installCommands = { windows: 'git clone https://github.com/Toh-ku/MClaude.git\nSet-Location MClaude\nuv sync --locked\nuv run mclaude', unix: 'git clone https://github.com/Toh-ku/MClaude.git\ncd MClaude\nuv sync --locked\nuv run mclaude' };
let activeOS = 'windows';
function setupTabs(selector, onSelect) {
  const tabs = [...document.querySelectorAll(selector)];
  function select(tab) { tabs.forEach(item => { const active = item === tab; item.setAttribute('aria-selected', String(active)); item.tabIndex = active ? 0 : -1; }); onSelect(tab); }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => select(tab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== undefined) { event.preventDefault(); tabs[next].focus(); select(tabs[next]); }
    });
  });
  select(tabs[0]);
}
setupTabs('[data-demo]', tab => { const example = examples[tab.dataset.demo]; document.querySelector('#demo-command').textContent = example.command; document.querySelector('#demo-content').innerHTML = example.content; document.querySelector('#demo').setAttribute('aria-labelledby', tab.id); });
setupTabs('[data-os]', tab => { activeOS = tab.dataset.os; document.querySelector('#install-command').textContent = installCommands[activeOS]; document.querySelector('#install-panel').setAttribute('aria-labelledby', tab.id); });
let toastTimer;
function showToast(message) { const toast = document.querySelector('#toast'); toast.textContent = message; toast.classList.add('visible'); clearTimeout(toastTimer); toastTimer = setTimeout(() => toast.classList.remove('visible'), 3000); }
document.querySelector('#copy-command').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(installCommands[activeOS]); showToast('安装命令已复制，可粘贴到终端。'); }
  catch { const range = document.createRange(); range.selectNodeContents(document.querySelector('#install-command')); const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range); showToast('请按 Ctrl+C（Mac 使用 ⌘C）复制选中的命令。'); }
});
