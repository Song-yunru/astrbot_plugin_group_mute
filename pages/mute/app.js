const bridge = window.AstrBotPluginPage;

const el = {
  list: document.getElementById("list"),
  message: document.getElementById("message"),
  refresh: document.getElementById("refresh"),
  total: document.getElementById("stat-total"),
  muted: document.getElementById("stat-muted"),
};

function showMessage(text, tone) {
  el.message.textContent = text || "";
  el.message.dataset.tone = tone || "error";
  el.message.hidden = !text;
}

function formatTime(seconds) {
  if (!seconds) return "—";
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return pad(date.getMonth() + 1) + "-" + pad(date.getDate()) + " " + pad(date.getHours()) + ":" + pad(date.getMinutes());
}

function renderRow(group) {
  const row = document.createElement("div");
  row.className = "row" + (group.muted ? " muted" : "");

  const info = document.createElement("div");
  info.className = "info";

  const name = document.createElement("p");
  name.className = "name";
  name.textContent = group.name || "未知群名";

  const meta = document.createElement("p");
  meta.className = "meta";
  meta.textContent = group.group_id + " · 消息 " + group.messages + " · 最近 " + formatTime(group.last_seen);

  info.appendChild(name);
  info.appendChild(meta);

  const label = document.createElement("label");
  label.className = "switch";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = !!group.muted;
  input.addEventListener("change", () => {
    void toggle(group.group_id, input.checked, input);
  });
  const track = document.createElement("span");
  track.className = "track";
  label.appendChild(input);
  label.appendChild(track);

  row.appendChild(info);
  row.appendChild(label);
  return row;
}

async function toggle(groupId, muted, input) {
  input.disabled = true;
  try {
    await bridge.apiPost("mute", { group_id: groupId, muted: muted });
    showMessage(muted ? "已静音该群" : "已取消静音", "ok");
    await load();
  } catch (error) {
    input.checked = !muted;
    showMessage(error instanceof Error ? error.message : "操作失败", "error");
  } finally {
    input.disabled = false;
  }
}

async function load() {
  const data = await bridge.apiGet("groups");
  const groups = Array.isArray(data && data.groups) ? data.groups : [];
  el.total.textContent = String(data && data.total ? data.total : groups.length);
  el.muted.textContent = String(data && data.muted ? data.muted : 0);

  el.list.textContent = "";
  if (!groups.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "还没记录到群聊。让机器人在群里收到一条消息后，再点刷新。";
    el.list.appendChild(empty);
    return;
  }
  const frag = document.createDocumentFragment();
  for (const group of groups) frag.appendChild(renderRow(group));
  el.list.appendChild(frag);
}

async function boot() {
  if (!bridge) {
    showMessage("页面桥接不可用，请从 AstrBot 面板打开本页面。", "error");
    return;
  }
  el.refresh.addEventListener("click", () => {
    void load().catch((error) => showMessage(error.message || "刷新失败", "error"));
  });
  try {
    const context = await bridge.ready();
    document.title = (context && context.pageTitle) || "群聊静音";
    await load();
  } catch (error) {
    showMessage(error instanceof Error ? error.message : "加载失败", "error");
  }
}

void boot();
