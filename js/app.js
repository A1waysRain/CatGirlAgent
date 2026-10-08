/**
 * 猫娘来咯 · 宽敞产品感主前端
 *
 * 双栏布局：侧栏（品牌/新对话/历史会话/功能入口）+ 主区（顶栏/聊天/输入）。
 * 多会话历史由后端持久化到 %APPDATA%\catgirl\sessions\<id>.json
 * （桌面壳每次启动端口随机，localStorage 按端口隔离不可靠，故历史放在后端文件）。
 * 所有业务逻辑走后端 /api/*；本文件只负责渲染与交互。
 */
(function () {
    "use strict";

    // ===== DOM 元素 =====
    const chatArea = document.getElementById("chatArea");
    const messageInput = document.getElementById("messageInput");
    const sendBtn = document.getElementById("sendBtn");
    const clearBtn = document.getElementById("clearBtn");
    const screenshotBtn = document.getElementById("screenshotBtn");
    const uploadImageBtn = document.getElementById("uploadImageBtn");
    const uploadFileBtn = document.getElementById("uploadFileBtn");

    const sessionList = document.getElementById("sessionList");
    const newChatBtn = document.getElementById("newChatBtn");
    const collapseBtn = document.getElementById("collapseBtn");
    const brandName = document.getElementById("brandName");
    const nameEditBtn = document.getElementById("nameEditBtn");
    const brandAvatar = document.getElementById("brandAvatar");
    const curTitle = document.getElementById("curTitle");
    const curMeta = document.getElementById("curMeta");
    const titleEditBtn = document.getElementById("titleEditBtn");
    const titlePop = document.getElementById("titlePop");
    const titlePopInput = document.getElementById("titlePopInput");
    const titlePopOk = document.getElementById("titlePopOk");
    const titlePopCancel = document.getElementById("titlePopCancel");

    const settingsOverlay = document.getElementById("settingsOverlay");
    const settingsCloseBtn = document.getElementById("settingsCloseBtn");
    const scanAppsBtn = document.getElementById("scanAppsBtn");
    const appScanInfo = document.getElementById("appScanInfo");
    const addPluginBtn = document.getElementById("addPluginBtn");
    const reloadPluginsBtn = document.getElementById("reloadPluginsBtn");
    const pluginList = document.getElementById("pluginList");
    const refreshMemoryBtn = document.getElementById("refreshMemoryBtn");
    const toggleMemoryBtn = document.getElementById("toggleMemoryBtn");
    const clearMemoryBtn = document.getElementById("clearMemoryBtn");
    const memoryList = document.getElementById("memoryList");
    const memoryDetails = document.getElementById("memoryDetails");
    const memorySessionSelect = document.getElementById("memorySessionSelect");
    const refreshContextItemsBtn = document.getElementById("refreshContextItemsBtn");
    const toggleContextItemsBtn = document.getElementById("toggleContextItemsBtn");
    const contextItemsDetails = document.getElementById("contextItemsDetails");
    const contextItemsList = document.getElementById("contextItemsList");
    const importRagFileBtn = document.getElementById("importRagFileBtn");
    const rebuildRagBtn = document.getElementById("rebuildRagBtn");
    const toggleRagBtn = document.getElementById("toggleRagBtn");
    const ragFileList = document.getElementById("ragFileList");
    const ragStatus = document.getElementById("ragStatus");
    const ragDetails = document.getElementById("ragDetails");
    const alarmList = document.getElementById("alarmList");
    const alarmTimeInput = document.getElementById("alarmTimeInput");
    const alarmMsgInput = document.getElementById("alarmMsgInput");
    const alarmDateInput = document.getElementById("alarmDateInput");
    const alarmRepeatSelect = document.getElementById("alarmRepeatSelect");
    const addAlarmBtn = document.getElementById("addAlarmBtn");
    const alarmToggleBtn = document.getElementById("alarmToggleBtn");
    const alarmToggleLabel = document.getElementById("alarmToggleLabel");
    const alarmWrap = document.getElementById("alarmWrap");
    const toast = document.getElementById("toast");
    const chatSearchInput = document.getElementById("chatSearchInput");
    const chatSearchResults = document.getElementById("chatSearchResults");

    // ===== 状态 =====
    let isWaiting = false;
    let settings = {};
    let userAvatarUrl = "/img/user.jpg";
    let catAvatarUrl = "/img/cat.png";
    let showTimestamp = false;
    let currentSessionId = null;
    let memoryViewSid = null;   // 记忆卡片当前查看的会话（下拉框选的，默认跟当前会话走）
    let memoryDetailsCollapsed = true;
    let contextItemsCollapsed = true;
    let ragDetailsCollapsed = true;
    let currentSessionTitle = "新会话";
    let appGuideShown = false;
    let remoteSessionListSignature = "";
    let remoteCurrentSessionRevision = "";
    // 当前最后一条猫娘回复的气泡元素（唯一可重新生成的那条，重生成只作用于它）
    let lastBotEl = null;

    // ===== 主题色 =====
    const THEME_PRESETS = ["#ec4899", "#f59e0b", "#10b981", "#3b82f6", "#8b5cf6", "#ef4444"];

    function hexToRgb(hex) {
        hex = hex.replace("#", "");
        if (hex.length === 3) hex = hex.split("").map(c => c + c).join("");
        const n = parseInt(hex, 16);
        return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
    }
    function mixColor(hex, target, ratio) {
        const c = hexToRgb(hex), t = hexToRgb(target);
        const r = Math.round(c.r + (t.r - c.r) * ratio);
        const g = Math.round(c.g + (t.g - c.g) * ratio);
        const b = Math.round(c.b + (t.b - c.b) * ratio);
        return "#" + ((1 << 24) | (r << 16) | (g << 8) | b).toString(16).slice(1);
    }
    function applyTheme(color) {
        const root = document.documentElement.style;
        root.setProperty("--accent", color);
        root.setProperty("--accent-deep", mixColor(color, "#000000", 0.28));
        root.setProperty("--accent-soft", mixColor(color, "#ffffff", 0.90));
        root.setProperty("--accent-softer", mixColor(color, "#ffffff", 0.96));
        root.setProperty("--accent-border", mixColor(color, "#ffffff", 0.80));
        const input = document.getElementById("themeColorInput");
        if (input) input.value = color;
        document.querySelectorAll("#themeSwatches .swatch").forEach(function (sw) {
            sw.classList.toggle("active", (sw.style.background || "").toLowerCase() === color.toLowerCase());
        });
    }

    // ===== 侧栏布局（收窄渐变 / 图标条 / 折叠 / 吸底对齐） =====
    function updateSidebarFade() {
        const sidebar = document.querySelector(".sidebar");
        if (!sidebar) return;
        const w = sidebar.getBoundingClientRect().width;
        const fade = Math.max(0, Math.min(1, (w - 80) / 160));
        sidebar.style.setProperty("--side-fade", fade.toFixed(3));
        // 纯图标条触发点跟随头像：侧栏实际宽度 ≤ 头像宽+图标条内边距 即收成图标条
        const root = getComputedStyle(document.documentElement);
        const avt = parseFloat(root.getPropertyValue("--avt")) || 44;
        const pad = parseFloat(root.getPropertyValue("--icon-pad-x")) || 6;
        sidebar.classList.toggle("icon-bar", w <= avt + 2 * pad + 0.5);
    }

    // 注：原先这里有个 alignFuncBottom()，把侧栏 padding-bottom 撑大，好让最后一个功能项
    // 的中心线与聊天输入框上沿齐平。副作用是侧栏底部留出一大片死白（高窗下约 200px），
    // 而历史会话区是 flex:1 —— 这块 padding 它吃不到，所以会话列表白白少一截。
    // 主人 2026-10-02 看示意图后拍板：去掉留白、功能区直接贴底，把空间还给历史会话区。

    // ===== 品牌改名（同步 pet_name 设置） =====
    function startRename() {
        brandName.contentEditable = "true";
        brandName.focus();
        try { document.execCommand("selectAll"); } catch (e) {}
    }
    function endRename() {
        brandName.contentEditable = "false";
        const val = brandName.textContent.trim() || "猫娘";
        brandName.textContent = val;
        const petNameInput = document.getElementById("petNameInput");
        if (petNameInput) petNameInput.value = val;
        saveSetting("pet_name", val);
        showToast("称呼已改成 " + val + " 喵");
    }

    // ===== 会话标题改名（顶栏 ✎ → 弹小窗）=====
    // 走 PUT /api/sessions/{sid}/title；后端会把该会话的 title_auto 置 False，
    // 所以主人亲手改过的标题不会再被"第一轮自动提炼"覆盖掉。
    // 为什么改成小窗：原来是在 <h2> 上直接 contenteditable，只有一圈虚线框、跟平时几乎没差别，
    // 主人反馈"点了几次体感不明显"——弹个小窗才一眼看得出进入了编辑态。
    function titlePopOpen() { return !titlePop.hidden; }

    function placeTitlePop() {
        // 贴在标题下方（左对齐标题、右/左越界时钳制回视窗内）
        const r = curTitle.getBoundingClientRect();
        const w = titlePop.offsetWidth || 272;
        let left = r.left;
        left = Math.max(10, Math.min(left, window.innerWidth - w - 10));
        titlePop.style.left = left + "px";
        titlePop.style.top = (r.bottom + 8) + "px";
    }

    function openTitlePop() {
        if (!currentSessionId) return;
        titlePop.hidden = false;
        titleEditBtn.classList.add("active");
        placeTitlePop();
        titlePopInput.value = currentSessionTitle || "";  // 带出当前标题，改起来有参照
        titlePopInput.focus();
        titlePopInput.select();
    }

    function closeTitlePop() {
        if (!titlePopOpen()) return;
        titlePop.hidden = true;
        titleEditBtn.classList.remove("active");
    }

    async function commitTitlePop() {
        const val = (titlePopInput.value || "").replace(/\s+/g, " ").trim();
        if (!val) {                       // 空白：不关窗，让他接着改（关掉才提示等于白改一次）
            showToast("标题不能为空喵～");
            titlePopInput.focus();
            return;
        }
        if (val === currentSessionTitle) { closeTitlePop(); return; }  // 没改，静默关掉
        const sid = currentSessionId;
        try {
            const r = await fetch("/api/sessions/" + encodeURIComponent(sid) + "/title", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ title: val }),
            });
            if (!r.ok) throw new Error("HTTP " + r.status);
            const saved = (await r.json()).session.title;
            // syncSessionTitle 会同时写顶栏和侧栏列表项；refreshSessionList 把 4s 轮询的
            // 比对基准刷新到最新，否则下一拍轮询会用旧签名把标题"闪"回旧值。
            syncSessionTitle({ id: sid, title: saved });
            refreshSessionList();
            closeTitlePop();
            showToast("标题已改成「" + saved + "」喵");
        } catch (e) {
            showToast("改标题失败了喵，再试一次？");
        }
    }

    // ===== 会话管理 =====
    function renderSidebarSessions(sessions, current) {
        sessionList.innerHTML = "";
        (sessions || []).forEach(function (s) {
            const item = document.createElement("div");
            item.className = "session-item" + (s.id === current ? " active" : "");
            item.dataset.id = s.id;
            item.title = s.title || "新会话";  // 窄窗图标条下 hover 显示会话名
            item.innerHTML =
                '<span class="ico"><img src="/img/icons/会话.png" alt="会话"></span>' +
                '<span class="tt">' + escapeHtml(s.title || "新会话") + '</span>' +
                '<button class="del" title="删除会话"><img src="/img/icons/删除.png" alt="删除会话"></button>';
            item.querySelector(".del").addEventListener("click", function (e) {
                e.stopPropagation();
                if (confirm("确定要删除这个会话吗喵？")) handleDeleteSession(s.id);
            });
            item.addEventListener("click", function () { handleSessionSwitch(s.id); });
            sessionList.appendChild(item);
        });
    }

    async function restoreSessions() {
        try {
            const resp = await fetch("/api/sessions");
            if (!resp.ok) return;
            const data = await resp.json();
            currentSessionId = data.current;
            renderSidebarSessions(data.sessions || [], data.current);
            remoteSessionListSignature = sessionListSignature(data.sessions || []);
            remoteCurrentSessionRevision = sessionRevision(data.sessions || [], currentSessionId);
            if (currentSessionId) {
                const r = await fetch("/api/sessions/" + currentSessionId);
                if (r.ok) {
                    const sdata = await r.json();
                    currentSessionTitle = sdata.session.title || "新对话";
                    renderHistory(sdata.session.messages || []);
                }
            }
        } catch (error) {
            // 后端未就绪等场景：保持欢迎语，静默即可
        }
    }

    async function refreshSessionList() {
        const r = await fetch("/api/sessions");
        if (!r.ok) return;
        const data = await r.json();
        currentSessionId = data.current;
        renderSidebarSessions(data.sessions || [], data.current);
        remoteSessionListSignature = sessionListSignature(data.sessions || []);
        remoteCurrentSessionRevision = sessionRevision(data.sessions || [], currentSessionId);
    }

    function sessionListSignature(sessions) {
        return (sessions || []).map(function (session) {
            return [session.id, session.title, session.count, session.updated_at].join("|");
        }).join(";");
    }

    function sessionRevision(sessions, sid) {
        const session = (sessions || []).find(function (item) { return item.id === sid; });
        return session ? [session.id, session.title, session.count, session.updated_at].join("|") : "";
    }

    let _syncSeq = 0;  // 远端同步轮次号：乱序防护用，见 syncRemoteSessions

    async function syncRemoteSessions() {
        // 4s 定时器与 visibilitychange 会并发进来，晚返回的旧请求不得覆盖较新的结果：
        // 用递增序号标记本轮，任何一个 await 之后若已被更新的轮次取代就丢弃。
        const seq = ++_syncSeq;
        // 手机与桌面共用磁盘会话，但各自保留正在看的会话，避免手机切换会话抢走桌面视图。
        if (document.hidden || isWaiting || !currentSessionId) return;
        // 打字机 reveal 尚未落库（assistant 气泡还没补 id）时跳过同步：服务端比 DOM 多出的
        // 那 1 条很可能就是它自己，贸然追尾会重复一条、下一拍 domGt 又整表重建（"甩到顶又滚回"）。
        if (chatArea.querySelector(".message[data-streaming]")) return;
        try {
            const response = await fetch("/api/sessions");
            if (!response.ok || isWaiting || seq !== _syncSeq) return;
            const data = await response.json();
            const sessions = data.sessions || [];
            const listSignature = sessionListSignature(sessions);
            if (seq !== _syncSeq) return;
            if (listSignature !== remoteSessionListSignature) {
                renderSidebarSessions(sessions, currentSessionId);
                remoteSessionListSignature = listSignature;
            }
            const revision = sessionRevision(sessions, currentSessionId);
            if (!revision || revision === remoteCurrentSessionRevision || isWaiting) return;
            const detailResponse = await fetch("/api/sessions/" + encodeURIComponent(currentSessionId));
            if (!detailResponse.ok || isWaiting || seq !== _syncSeq) return;
            const detail = await detailResponse.json();
            if (detail.session.id !== currentSessionId || isWaiting || seq !== _syncSeq) return;
            // ★标题变了必须自己写 DOM：applyRemoteSessionMessages 在消息数没变时会提前
            // return，光赋值不写 DOM 的话，自动提炼出来的新标题在顶栏根本看不见。
            // （改名小窗开着时照写不误——他编辑的是小窗里的输入框，不是顶栏文字。）
            const remoteTitle = detail.session.title || "新对话";
            currentSessionTitle = remoteTitle;
            if (curTitle.textContent !== remoteTitle) {
                curTitle.textContent = remoteTitle;
            }
            applyRemoteSessionMessages(detail.session.messages || []);
            if (seq === _syncSeq) remoteCurrentSessionRevision = revision;
        } catch (error) {
            // 短暂断网不影响当前已打开的聊天内容，下一轮再同步。
        }
    }

    async function handleSessionSwitch(sid) {
        if (!sid || sid === currentSessionId) return;
        if (isWaiting) return;  // 回复中不允许切换
        resetSearch();  // 搜索结果只属于当前会话，切换后清掉
        try {
            const r = await fetch("/api/sessions/" + encodeURIComponent(sid));
            if (!r.ok) return;
            const data = await r.json();
            currentSessionId = sid;
            currentSessionTitle = data.session.title || "新对话";
            renderHistory(data.session.messages || []);
            if (settingsOverlay.classList.contains("open")) syncMemoryToCurrent();  // 记忆下拉框回到当前会话
            await fetch("/api/sessions/current", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ session_id: sid }),
            });
            refreshSessionList();
            messageInput.focus();
        } catch (error) {
            await refreshSessionList();
        }
    }

    async function handleNewSession() {
        if (isWaiting) return;
        resetSearch();  // 新会话无搜索结果
        try {
            await fetch("/api/sessions", { method: "POST" });
            await refreshSessionList();
            const r = await fetch("/api/sessions/" + currentSessionId);
            const data = await r.json();
            currentSessionTitle = data.session.title || "新对话";
            renderHistory(data.session.messages || []);
            messageInput.focus();
        } catch (error) {
            // 后端异常：保持现状
        }
    }

    async function handleDeleteSession(sid) {
        if (isWaiting) return;
        const target = sid || currentSessionId;
        if (!target) return;
        resetSearch();  // 会话变了，旧搜索结果作废
        try {
            const r = await fetch("/api/sessions/" + encodeURIComponent(target), { method: "DELETE" });
            if (!r.ok) return;
            const wasCurrent = target === currentSessionId;
            await refreshSessionList();
            // 删的是非当前会话：当前会话消息没变，不用整列表重建（长聊天下避免"重新加载"）
            if (wasCurrent) {
                const rr = await fetch("/api/sessions/" + currentSessionId);
                if (rr.ok) {
                    const sdata = await rr.json();
                    currentSessionTitle = sdata.session.title || "新对话";
                    renderHistory(sdata.session.messages || []);
                }
            }
            messageInput.focus();
        } catch (error) {
            // 后端异常：保持现状
        }
    }

    // ===== 清空当前会话 =====
    async function handleClear() {
        if (isWaiting) return;
        if (!confirm("确定要清空当前会话的消息吗喵？")) return;
        resetSearch();  // 清空后搜索结果作废
        try {
            await fetch("/api/history", { method: "DELETE" });
        } catch (error) {
            // 后端异常也照常清空界面
        }
        renderHistory([]);
        await refreshSessionList();
        if (settingsOverlay.classList.contains("open")) syncMemoryToCurrent();  // 清空后记忆小本本也没了
        messageInput.focus();
    }

    // 消息列表有增减后的收尾：把 🔄 钮挂到最后一条可重生成的猫娘回复、空会话欢迎态、同步计数。
    // 历史很长时避免整列表重建（会肉眼可见地"刷新"一遍，弱机器上甚至可能拖垮渲染器）。
    function finalizeMessageList() {
        chatArea.querySelectorAll(".msg-regen").forEach(function (el) { el.remove(); });
        lastBotEl = null;
        const bots = chatArea.querySelectorAll(".message.bot[data-msg-id]");
        if (bots.length) {
            lastBotEl = bots[bots.length - 1];
            const rb = document.createElement("button");
            rb.className = "msg-regen";
            rb.title = "重新生成回答";
            rb.innerHTML = '<img src="/img/icons/刷新.png" alt="重新生成回答">';
            rb.addEventListener("click", handleRegenerate);
            lastBotEl.appendChild(rb);
        }
        if (!chatArea.querySelector(".message")) {
            removeWelcome();
            welcome();
        }
        refreshMeta();
    }

    // ===== 删除单轮对话 =====
    async function handleDeleteRound(messageId) {
        if (isWaiting) return;
        if (!messageId || !currentSessionId) return;
        if (!confirm("要删除这一轮对话吗喵？")) return;
        try {
            const r = await fetch(
                "/api/sessions/" + currentSessionId + "/messages/" + encodeURIComponent(messageId),
                { method: "DELETE" }
            );
            if (!r.ok) return;
            const data = await r.json();
            currentSessionTitle = data.session.title || "新对话";
            // 增量更新 DOM：按后端返回的剩余消息 id 对账，只移除被删那一轮对应的元素，不整列表重建。
            const keepIds = new Set((data.session.messages || []).map(function (m) { return m.id; }));
            chatArea.querySelectorAll(".message[data-msg-id]").forEach(function (el) {
                if (!keepIds.has(el.dataset.msgId)) el.remove();
            });
            finalizeMessageList();
            syncSessionTitle(data.session);
            await refreshSessionList();
            messageInput.focus();
        } catch (error) {
            // 后端异常：保持现状
        }
    }

    // ===== 重新生成回答（只作用于最后一条猫娘回复） =====
    async function handleRegenerate() {
        if (isWaiting) return;
        const target = lastBotEl;
        if (!target || !currentSessionId) return;
        // 先把旧回答摘走（视觉上开始重新生成）。后端在 error/断连时会 restore_assistant
        // 把旧答存回会话文件，但前端 DOM 已删，所以要记住旧元素，失败时放回去——
        // 否则"点重新生成失败 = 旧回答从眼前消失"（app.js优化建议 #7）。
        const saved = { target: target, parent: target.parentNode, next: target.nextSibling };
        target.remove();
        lastBotEl = null;
        setWaiting(true);
        const typingMsg = appendTyping();
        petEvent("emotion", { mood: "think" });
        // 恢复旧回答；旧元素还挂着 🔄/🗑 钮，可继续重生成/删除
        function restoreOldAnswer() {
            if (saved.parent && saved.target.parentNode !== saved.parent) {
                saved.parent.insertBefore(saved.target, saved.next);
                lastBotEl = saved.target;
            }
        }
        try {
            const streamBot = createStreamingBot(typingMsg);
            await fetchSSE("/api/sessions/" + currentSessionId + "/regenerate", {
                method: "POST",
            }, {
                onMeta(evt) {
                    // regenerate 没有新 user 消息，meta 里 user 为 null；只同步标题
                    if (evt.session) syncSessionTitle(evt.session);
                },
                onStatus(evt) {
                    const text = (evt && evt.text) || "正在操作电脑喵…";
                    if (typingMsg && typingMsg.parentNode) {
                        const b = typingMsg.querySelector(".msg-bubble");
                        if (b) b.textContent = text;
                    } else {
                        showToast(text);
                    }
                },
                onDelta(evt) { streamBot.delta(evt.text); },
                onDone(evt)  { streamBot.done(evt.message_ids && evt.message_ids.assistant); },
                onError(detail) {
                    // 旧回答能恢复就恢复它，不给错误气泡（错误用 toast 提示）；
                    // 旧 DOM 已不可用时才退回错误气泡
                    if (saved.parent && saved.target.parentNode !== saved.parent) {
                        streamBot.abandon();
                        restoreOldAnswer();
                        showToast("重新生成失败了喵，已恢复原来的回答");
                    } else {
                        streamBot.fail(detail);
                    }
                    refreshMeta();
                },
            });
        } catch (error) {
            restoreOldAnswer();
            typingMsg.remove();
            appendMessage("bot", "本喵的网络好像出问题了喵…等会儿再试试喵~");
            petEvent("emotion", { mood: "happy" });
        } finally {
            refreshMeta();
            setWaiting(false);
        }
    }

    // 会话标题同步（重生成/发送后，顶栏标题 + 侧栏列表项文字）
    function syncSessionTitle(session) {
        if (!session) return;
        if (session.id === currentSessionId) {
            currentSessionTitle = session.title || "新对话";
            curTitle.textContent = currentSessionTitle;
        }
        const item = sessionList.querySelector('.session-item[data-id="' + session.id + '"] .tt');
        if (item) item.textContent = session.title || "新会话";
    }

    // ===== 消息渲染 =====
    function nowTime() {
        return new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    }

    function formatBotMessage(text) {
        let html = escapeHtml(text);
        // 最基础的两条 markdown：行首 `#` 标题 → 加粗；行首 `- / * / •` → 圆点。
        // 起因（2026-09-27）：猫娘在长清单里用了 `## 小标题` 和 `- 列表项`，而这里只认
        // `（动作）` 与 `**加粗**` → 主人屏幕上看到的是字面的 "## " 和 "- "，观感崩了。
        // 顺序在 escapeHtml 之后、bold 之前：只加我们自己的标签，不引入任何原文 HTML。
        html = html.replace(/^[ \t]*#{1,6}[ \t]*(.+)$/gm, "<strong>$1</strong>");
        html = html.replace(/^[ \t]*[-*•][ \t]+/gm, "• ");
        html = html.replace(/（([^）]+)）/g, '<span class="action-text">（$1）</span>');
        html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
        return html;
    }

    function escapeHtml(text) {
        const div = document.createElement("div");
        div.textContent = text;
        return div.innerHTML;
    }

    // 给一条消息挂操作钮（删除单轮 / 重新生成）；流式气泡落库后再补挂
    function addRoundButtons(wrap, msgId, canRegenerate) {
        if (!msgId) return;
        wrap.dataset.msgId = msgId;
        const delBtn = document.createElement("button");
        delBtn.className = "msg-del";
        delBtn.title = "删除这一轮";
        delBtn.innerHTML = '<img src="/img/icons/删除.png" alt="删除这一轮">';
        delBtn.addEventListener("click", function () { handleDeleteRound(msgId); });
        wrap.appendChild(delBtn);
        if (canRegenerate) {
            const regenBtn = document.createElement("button");
            regenBtn.className = "msg-regen";
            regenBtn.title = "重新生成回答";
            regenBtn.innerHTML = '<img src="/img/icons/刷新.png" alt="重新生成回答">';
            regenBtn.addEventListener("click", handleRegenerate);
            wrap.appendChild(regenBtn);
        }
    }

    function appendFactPanel(body, msgId, factRefs, factMeta) {
        if (!factMeta || !factMeta.query || !factRefs || !factRefs.length) return;
        const panel = document.createElement("details");
        panel.className = "fact-panel";
        const summary = document.createElement("summary");
        const status = factMeta.status === "verified" ? "已找到" : "证据有限";
        summary.textContent = "联网核验 · " + status + " " + factRefs.length + " 个来源";
        panel.appendChild(summary);
        const info = document.createElement("div");
        info.className = "fact-meta";
        info.textContent = "核验时间：" + (factMeta.checked_at || "未知") +
            "　正文证据：" + (factMeta.full || 0) + " 条　" +
            "独立来源：" + (factMeta.independent_domains || 0) + " 个";
        panel.appendChild(info);
        const list = document.createElement("div");
        list.className = "fact-sources";
        factRefs.forEach(function (ref) {
            const row = document.createElement("a");
            row.className = "fact-source";
            row.href = ref.url; row.target = "_blank"; row.rel = "noopener";
            row.textContent = (ref.domain || "未知来源") + " · " + (ref.grade || "none");
            row.title = ref.title || ref.url;
            list.appendChild(row);
        });
        panel.appendChild(list);
        const refresh = document.createElement("button");
        refresh.className = "fact-refresh";
        refresh.type = "button";
        refresh.textContent = "重新核验";
        refresh.addEventListener("click", async function () {
            refresh.disabled = true; refresh.textContent = "核验中…";
            try {
                const r = await fetch("/api/sessions/" + encodeURIComponent(currentSessionId) +
                    "/messages/" + encodeURIComponent(msgId) + "/fact-refresh", { method: "POST" });
                const data = await r.json();
                if (!r.ok) throw new Error(data.detail || "刷新失败");
                const parent = panel.parentNode;
                panel.remove();
                appendFactPanel(parent, msgId, data.fact_refs, data.fact_meta);
                showToast("来源已重新核验喵");
            } catch (e) { showToast(e.message || "重新核验失败喵"); }
            finally { refresh.disabled = false; refresh.textContent = "重新核验"; }
        });
        panel.appendChild(refresh);
        body.appendChild(panel);
    }

    function appendMessage(role, text, msgId, canRegenerate, factRefs, factMeta) {
        const wrap = document.createElement("div");
        wrap.className = "message " + role;

        const av = document.createElement("div");
        av.className = "avatar msg-avatar";
        const avImg = document.createElement("img");
        avImg.src = role === "user" ? userAvatarUrl : catAvatarUrl;
        avImg.alt = "";
        av.appendChild(avImg);
        wrap.appendChild(av);

        const body = document.createElement("div");
        body.className = "msg-body";

        if (showTimestamp) {
            const t = document.createElement("div");
            t.className = "msg-time";
            t.textContent = nowTime();
            body.appendChild(t);
        }
        const bubble = document.createElement("div");
        bubble.className = "msg-bubble";
        bubble.innerHTML = role === "bot" ? formatBotMessage(text) : escapeHtml(text);
        body.appendChild(bubble);
        if (role === "bot") appendFactPanel(body, msgId, factRefs, factMeta);
        wrap.appendChild(body);

        // 操作钮：只有真实落库的消息（有 id）才有；user 的 row-reverse 自动落气泡外侧
        addRoundButtons(wrap, msgId, canRegenerate);

        chatArea.appendChild(wrap);
        chatArea.scrollTop = chatArea.scrollHeight;
        return wrap;
    }

    function renderHistory(history, preserveScroll) {
        const previousScrollTop = chatArea.scrollTop;
        const wasNearBottom = chatArea.scrollHeight - chatArea.scrollTop - chatArea.clientHeight < 40;
        chatArea.querySelectorAll(".message").forEach(function (el) {
            el.remove();
        });
        removeWelcome();
        lastBotEl = null;
        const msgs = history || [];
        curTitle.textContent = currentSessionTitle || "新对话";
        if (!msgs.length) { welcome(); refreshMeta(); return; }
        msgs.forEach(function (m, i) {
            // 只有最后一条猫娘回复可以重新生成
            const isLastAssistant = (m.role === "assistant") && (i === msgs.length - 1);
            const el = appendMessage(m.role === "user" ? "user" : "bot", m.content, m.id, isLastAssistant, m.fact_refs, m.fact_meta);
            if (isLastAssistant) lastBotEl = el;
        });
        refreshMeta();
        if (!preserveScroll || wasNearBottom) {
            chatArea.scrollTop = chatArea.scrollHeight;
        } else {
            chatArea.scrollTop = Math.min(previousScrollTop, chatArea.scrollHeight - chatArea.clientHeight);
        }
    }

    // 远端（手机/后端）改动了当前会话时的"增量对账"，不整表重建——整表清空重建会让几百条
    // 消息一起重放 rise 入场动画（视觉跳变）且拖累渲染。只对"真·分叉"（他端删了中间/重生成
    // 换了 id，靠删头+追尾解释不了）才退回 renderHistoryQuiet 静默重建。
    // 对账按消息 id：
    //   纯追加：DOM 是服务端前缀（L=0）→ 只追尾部；
    //   截头：会话顶到 MAX_STORED=200 上限，服务端每追加就丢最老 N 条，DOM 前缀错位，
    //     但找得到"删掉最老 L 条后能对齐" → 只删 DOM 最老 L 条再追尾（滚动从原位置扣掉删掉的高度）；
    //   空欢迎态：DOM 无消息、服务端有 → 全部追。
    function applyRemoteSessionMessages(msgs) {
        const serverMsgs = msgs || [];
        const els = Array.prototype.slice.call(chatArea.querySelectorAll(".message[data-msg-id]"));
        const domIds = els.map(function (el) { return el.dataset.msgId; });
        const srvLen = serverMsgs.length;

        // 1) 服务端空了：DOM 也空就无事；DOM 还有（他端清了会话）→ 全删 + 欢迎态
        if (!srvLen) {
            if (!domIds.length) return;
            els.forEach(function (el) { el.remove(); });
            removeWelcome();
            welcome();
            refreshMeta();
            return;
        }

        // 2) 求"删掉 DOM 最老 L 条后，DOM 前缀能与服务端对齐"的 L。uuid 唯一 → 服务端首条
        //    在 DOM 里的位置就是 L（L=0 = 纯追加/相等）。
        let L = 0, domTail = 0;
        if (domIds.length) {
            L = domIds.indexOf(serverMsgs[0].id);
            if (L < 0) { renderHistoryQuiet(serverMsgs); return; }          // 服务端首条 DOM 里都没有 → 真分叉
            domTail = domIds.length - L;
            if (srvLen < domTail) { renderHistoryQuiet(serverMsgs); return; } // 服务端比对齐后还短 → 无法增量
            for (let i = 0; i < domTail; i++) {
                if (domIds[L + i] !== serverMsgs[i].id) { renderHistoryQuiet(serverMsgs); return; }
            }
            if (L === 0 && srvLen === domIds.length) return;                 // 完全没变化，DOM 一个都不碰
        }

        // 3) 删掉不再存在的 DOM 最老 L 条（顶到上限被服务端截头时才会 L>0）
        const prevTop = chatArea.scrollTop;
        const atBottom = chatArea.scrollHeight - chatArea.scrollTop - chatArea.clientHeight < 40;
        let removedTop = 0;
        if (L > 0) {
            removedTop = els.slice(0, L).reduce(function (sum, el) { return sum + el.offsetHeight; }, 0);
            els.slice(0, L).forEach(function (el) { el.remove(); });
        }

        // 4) 空欢迎态追首条前先请走欢迎
        if (!domIds.length) removeWelcome();

        // 5) 追服务端多出来的尾部（追尾期间 appendMessage 每次会滚到底，最后统一收）
        const start = domTail;
        if (srvLen > start) {
            serverMsgs.slice(start).forEach(function (m) {
                appendMessage(m.role === "user" ? "user" : "bot", m.content, m.id, false, m.fact_refs, m.fact_meta);
            });
        }

        // 6) 🔄 只挂到真正的最后一条猫娘回复上（可能在被追加的尾部，也可能仍是旧的最后一条）
        chatArea.querySelectorAll(".msg-regen").forEach(function (el) { el.remove(); });
        lastBotEl = null;
        const lastMsg = serverMsgs[srvLen - 1];
        if (lastMsg && lastMsg.role === "assistant") {
            const lastEl = chatArea.querySelector('.message[data-msg-id="' + lastMsg.id + '"]');
            if (lastEl) {
                const rb = document.createElement("button");
                rb.className = "msg-regen";
                rb.title = "重新生成回答";
                rb.innerHTML = '<img src="/img/icons/刷新.png" alt="重新生成回答">';
                rb.addEventListener("click", handleRegenerate);
                lastEl.appendChild(rb);
                lastBotEl = lastEl;
            }
        }
        refreshMeta();

        // 7) 滚动：删掉的顶部高度从原位置里扣掉；原在底部就贴新底
        let target;
        if (atBottom) {
            target = chatArea.scrollHeight;
        } else {
            target = Math.max(0, prevTop - removedTop);
            target = Math.min(target, chatArea.scrollHeight - chatArea.clientHeight);
        }
        chatArea.scrollTop = target;
    }

    // 同步兜底的真·整表重建也做成"静默"：重建期间临时摘掉 .message 入场动画，
    // 避免几百条 rise 动画同时重播（手机清了会话等真正的分叉需要重建时也不闪屏）。
    function renderHistoryQuiet(msgs) {
        chatArea.classList.add("no-anim");
        try {
            renderHistory(msgs, true);
        } finally {
            chatArea.classList.remove("no-anim");
        }
    }

    // 顶栏消息计数（发消息/重生成/删除后同步；typing 指示器不计入）
    function refreshMeta() {
        const n = chatArea.querySelectorAll(".message").length;
        curMeta.textContent = n ? n + " 条消息" : "空会话";
    }

    // ===== 搜索聊天记录（当前会话内） =====
    let searchTimer = null;
    let searchHitTimer = null;
    let searchRequestId = 0;  // 搜索请求序号：旧请求慢返回时丢弃，防旧结果盖新结果（app.js优化建议 #4）

    function clearSearchPanel() {
        if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
        chatSearchResults.innerHTML = "";
        chatSearchResults.hidden = true;
    }

    function resetSearch() {
        if (chatSearchInput) chatSearchInput.value = "";
        clearSearchPanel();
    }

    async function performSearch() {
        const q = chatSearchInput.value.trim();
        if (!q || !currentSessionId) { clearSearchPanel(); return; }
        const requestId = ++searchRequestId;
        try {
            const r = await fetch(
                "/api/sessions/" + encodeURIComponent(currentSessionId) + "/search?q=" + encodeURIComponent(q)
            );
            if (!r.ok || requestId !== searchRequestId) return;
            const data = await r.json();
            if (requestId !== searchRequestId) return;
            renderSearchResults(data.results || [], q);
        } catch (error) {
            if (requestId === searchRequestId) clearSearchPanel();
        }
    }

    // 结果面板按输入框位置 fixed 定位（侧栏 overflow:hidden 会裁掉 absolute 面板）
    function positionSearchPanel() {
        const rect = chatSearchInput.getBoundingClientRect();
        chatSearchResults.style.top = (rect.bottom + 8) + "px";
        chatSearchResults.style.left = rect.left + "px";
    }

    // 摘要片段：截取命中词附近的文字并高亮命中词（保留原大小写）
    function searchSnippet(content, query) {
        const lower = content.toLowerCase();
        const i = lower.indexOf(query.toLowerCase());
        const start = Math.max(0, i - 18);
        const end = Math.min(content.length, i + query.length + 62);
        const slice = (start > 0 ? "…" : "") + content.slice(start, end) + (end < content.length ? "…" : "");
        const esc = escapeHtml(slice);
        if (i >= 0) {
            const hit = escapeHtml(content.slice(i, i + query.length));
            return esc.split(hit).join("<mark>" + hit + "</mark>");
        }
        return esc;
    }

    function renderSearchResults(results, query) {
        chatSearchResults.innerHTML = "";
        positionSearchPanel();
        chatSearchResults.hidden = false;
        if (!results.length) {
            const empty = document.createElement("div");
            empty.className = "search-result-empty";
            empty.textContent = "没有找到相关消息喵";
            chatSearchResults.appendChild(empty);
            return;
        }
        results.forEach(function (m) {
            const row = document.createElement("div");
            row.className = "search-result-item";
            const ico = document.createElement("div");
            ico.className = "sri-ico";
            const im = document.createElement("img");
            im.src = m.role === "user" ? userAvatarUrl : catAvatarUrl;
            im.alt = "";
            ico.appendChild(im);
            const body = document.createElement("div");
            body.className = "sri-body";
            const txt = document.createElement("div");
            txt.className = "sri-text";
            txt.innerHTML = searchSnippet(m.content || "", query);
            const role = document.createElement("div");
            role.className = "sri-role";
            role.textContent = m.role === "user" ? "主人" : "猫娘";
            body.appendChild(txt);
            body.appendChild(role);
            row.appendChild(ico);
            row.appendChild(body);
            row.addEventListener("click", function () { jumpToSearchHit(m.message_id); });
            chatSearchResults.appendChild(row);
        });
    }

    function jumpToSearchHit(messageId) {
        if (!messageId) return;
        const target = chatArea.querySelector('.message[data-msg-id="' + messageId + '"]');
        resetSearch();
        chatSearchInput.blur();
        if (!target) return;
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        const bubble = target.querySelector(".msg-bubble");
        if (bubble) {
            bubble.classList.add("search-hit");
            if (searchHitTimer) clearTimeout(searchHitTimer);
            searchHitTimer = setTimeout(function () { bubble.classList.remove("search-hit"); }, 2000);
        }
    }

    function welcome() {
        const w = document.createElement("div");
        w.className = "welcome";
        w.innerHTML =
            '<div class="paw">🐾</div>' +
            '<div class="quote">哼，你终于来了喵～</div>' +
            '<div class="sub">今天也要一起，把活儿干得漂漂亮亮</div>' +
            '<div class="hint">✦ 侧栏里有会话和功能，怎么都不迷路</div>';
        chatArea.appendChild(w);
    }

    function removeWelcome() {
        const w = chatArea.querySelector(".welcome");
        if (w) w.remove();
    }

    function appendTyping() {
        const wrap = document.createElement("div");
        wrap.className = "message bot";
        const av = document.createElement("div");
        av.className = "avatar msg-avatar";
        const avImg = document.createElement("img");
        avImg.src = catAvatarUrl;
        avImg.alt = "";
        av.appendChild(avImg);
        wrap.appendChild(av);
        const body = document.createElement("div");
        body.className = "msg-body";
        body.innerHTML = '<div class="msg-bubble"><div class="typing"><i></i><i></i><i></i></div></div>';
        wrap.appendChild(body);
        chatArea.appendChild(wrap);
        chatArea.scrollTop = chatArea.scrollHeight;
        return wrap;
    }

    // ===== 流式输出（SSE） =====
    // 统一 send/regenerate 两条路的流式渲染：delta 累积全文重设 innerHTML，done 补 id+按钮
    function createStreamingBot(typingMsg) {
        // 打字机式显示：SSE 增量照常快速接收（进 buffer），但按固定节奏逐字 reveal，
        // 避免 DeepSeek 流太快时"一口气全出"看不清。想调速度改 STEP / INTERVAL。
        let el = null, buffer = "", shown = 0, timer = null, pendingDone = null;
        const STEP = 2;        // 每次 reveal 的字符数
        const INTERVAL = 25;   // 每多少毫秒 reveal 一次（≈80 字/秒）
        const removeTyping = function () {
            if (typingMsg && typingMsg.parentNode) typingMsg.remove();
        };
        function ensureEl() {
            if (!el) {
                removeTyping();
                el = appendMessage("bot", "");
                el.dataset.streaming = "1";  // 逐字 reveal 标记：期间同步跳过，见 syncRemoteSessions
            }
            return el;
        }
        function paint() {
            const bubble = el.querySelector(".msg-bubble");
            if (bubble) bubble.innerHTML = formatBotMessage(buffer.slice(0, shown));  // 只画已 reveal 的部分
            chatArea.scrollTop = chatArea.scrollHeight;
        }
        function tick() {
            timer = null;
            if (shown < buffer.length) {
                shown = Math.min(shown + STEP, buffer.length);
                paint();
                timer = setTimeout(tick, INTERVAL);
            } else if (pendingDone) {
                const cb = pendingDone; pendingDone = null;
                cb();
            }
        }
        function kick() {
            // 有增量就启动 reveal；已在跑就等 tick 自己接着来
            if (!timer) {
                shown = Math.min(shown + STEP, buffer.length);
                paint();
                if (shown < buffer.length) timer = setTimeout(tick, INTERVAL);
            }
        }
        return {
            delta(text) {
                ensureEl();
                buffer += text || "";
                kick();
            },
            done(assistantId, afterFinish) {
                const finish = function () {
                    removeTyping();
                    if (!el && buffer) el = appendMessage("bot", buffer);
                    if (el && assistantId) {
                        addRoundButtons(el, assistantId, true);  // 补 id + 删除/重生成钮
                        lastBotEl = el;
                    } else {
                        lastBotEl = null;
                    }
                    if (el) delete el.dataset.streaming;  // reveal 收尾，允许同步恢复
                    refreshMeta();
                    // 逐字 reveal 收尾后才轮到"挂在气泡上的东西"（来源面板等）——
                    // 不能在 onDone 里直接挂：reveal 没跑完时 lastBotEl 还是 null
                    if (typeof afterFinish === "function") afterFinish(el);
                };
                // 等 buffer 全部 reveal 完再补按钮（不然按钮出现在"字还没吐完"的时候很怪）
                if (timer || shown < buffer.length) pendingDone = finish;
                else finish();
            },
            fail(detail) {
                if (timer) { clearTimeout(timer); timer = null; }
                pendingDone = null;
                removeTyping();
                if (el) {
                    delete el.dataset.streaming;  // 出错：不再逐字 reveal，同步可恢复
                    const bubble = el.querySelector(".msg-bubble");
                    if (bubble) bubble.innerHTML = formatBotMessage(detail);
                    lastBotEl = null;
                } else {
                    appendMessage("bot", detail);
                }
                petEvent("emotion", { mood: "happy" });
                refreshMeta();
            },
            abandon() {
                // 重新生成失败走"恢复旧回答"时用：清掉打字机/半成品气泡，但不渲染错误文本
                if (timer) { clearTimeout(timer); timer = null; }
                pendingDone = null;
                removeTyping();
                if (el) el.remove();
                refreshMeta();
            },
        };
    }

    // 用 fetch 读 SSE 流。EventSource 只支持 GET，本接口是 POST → 必须 fetch + ReadableStream。
    // 返回 200 即开始读 body；!ok 时后端是 JSON 错误体，取 detail。
    async function fetchSSE(url, options, handlers) {
        // 【二十二点】兜底默认 handler：调用方漏传 onError 等也不会崩
        handlers = {
            onMeta: () => {}, onStatus: () => {}, onDelta: () => {},
            onDone: () => {}, onError: () => {}, ...handlers,
        };
        // 结束标记（app.js优化建议 #6）：error/done 只收一次，防止"error 事件 + 读流断开"
        // 等路径多次触发 onError，导致 streamBot.fail() 重复渲染错误气泡。
        let finished = false;
        function emitError(detail) {
            if (finished) return;
            finished = true;
            handlers.onError(detail);
        }
        function emitDone(evt) {
            if (finished) return;
            finished = true;
            handlers.onDone(evt);
        }
        // 解析并分发一个完整 SSE 事件块（"data: {...}\n..."，可多行）
        // 业务回调单独保护：渲染报错只提示"页面处理出错"，不误判成网络错误【二十一点】
        function dispatchBlock(block) {
            for (const rawLine of block.split("\n")) {
                const line = rawLine.trim();
                if (!line.startsWith("data:")) continue;
                const payload = line.slice(5).trim();
                if (!payload || payload === "[DONE]") continue;
                let evt;
                try { evt = JSON.parse(payload); } catch (e) { continue; }
                try {
                    if (evt.type === "meta" && handlers.onMeta) handlers.onMeta(evt);
                    else if (evt.type === "status" && handlers.onStatus) handlers.onStatus(evt);
                    else if (evt.type === "delta" && handlers.onDelta) handlers.onDelta(evt);
                    else if (evt.type === "done" && handlers.onDone) emitDone(evt);
                    else if (evt.type === "error" && handlers.onError) emitError(evt.detail || "出错了喵");
                } catch (e) {
                    console.error("SSE handler failed:", e);
                    emitError("页面处理消息时出错了喵");
                }
            }
        }
        let resp;
        try { resp = await fetch(url, options); }
        catch (e) { emitError("本喵的网络好像出问题了喵…等会儿再试试喵~"); return; }
        if (!resp.ok) {
            let detail = "本喵暂时不想理你喵~";
            try { detail = (await resp.json()).detail || detail; } catch (e) {}
            emitError(detail);
            return;
        }
        if (!resp.body) { emitError("没有响应内容喵"); return; }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        try {
            while (true) {
                const r = await reader.read();
                if (r.done) {
                    buf += decoder.decode();            // 【十九点】flush 解码器缓存，防最后一个字被截
                    break;
                }
                buf += decoder.decode(r.value, { stream: true });
                buf = buf.replace(/\r\n/g, "\n");       // CRLF 归一
                const blocks = buf.split("\n\n");       // SSE 事件分隔
                buf = blocks.pop();                      // 尾段可能是半行，留到下次拼
                for (const block of blocks) dispatchBlock(block);
            }
            if (buf.trim()) dispatchBlock(buf);          // 【十八点】流结束兜底：服务端没发结尾空行也不丢最后一段
        } catch (e) {
            // 走到这里才是真·网络/读流错误（业务回调已被 dispatchBlock 单独兜住）
            emitError("本喵的网络好像出问题了喵…等会儿再试试喵~");
        } finally {
            reader.releaseLock();
        }
    }

    // ===== 截图 / 上传图片 / 上传文件 =====
    function isImageUrl(url) {
        return /\.(png|jpe?g|gif|webp|bmp)$/i.test(url.split(/[?#]/)[0]);
    }

    function mediaAvatar() {
        const av = document.createElement("div");
        av.className = "avatar msg-avatar";
        const img = document.createElement("img");
        img.src = userAvatarUrl;
        img.alt = "";
        av.appendChild(img);
        return av;
    }

    function appendImage(url) {
        removeWelcome();
        const wrap = document.createElement("div");
        wrap.className = "message user";
        const body = document.createElement("div");
        body.className = "msg-body";
        const img = document.createElement("img");
        img.className = "chat-image";
        img.src = url;
        img.alt = "图片";
        img.addEventListener("click", function () { window.open(url, "_blank"); });
        body.appendChild(img);
        wrap.appendChild(mediaAvatar());
        wrap.appendChild(body);
        chatArea.appendChild(wrap);
        chatArea.scrollTop = chatArea.scrollHeight;
    }

    function appendFile(url, name) {
        removeWelcome();
        const wrap = document.createElement("div");
        wrap.className = "message user";
        const body = document.createElement("div");
        body.className = "msg-body";
        if (isImageUrl(url)) {
            const img = document.createElement("img");
            img.className = "chat-image";
            img.src = url;
            img.alt = name || "图片";
            img.addEventListener("click", function () { window.open(url, "_blank"); });
            body.appendChild(img);
        } else {
            const a = document.createElement("a");
            a.className = "chat-file";
            a.href = url;
            a.target = "_blank";
            a.title = "点击打开/下载";
            a.textContent = "📎 " + (name || "文件");
            body.appendChild(a);
        }
        wrap.appendChild(mediaAvatar());
        wrap.appendChild(body);
        chatArea.appendChild(wrap);
        chatArea.scrollTop = chatArea.scrollHeight;
    }

    async function handleScreenshot() {
        if (isWaiting) return;
        if (!window.pywebview || !window.pywebview.api) {
            alert("截图需要桌面版（pywebview）环境喵");
            return;
        }
        const path = await window.pywebview.api.take_screenshot();
        if (!path) return;  // 用户取消
        const url = "/media/" + path.split(/[\\/]/).pop();
        appendImage(url);
        await sendUserMessage("我截了一张图，图片路径是：" + path + "。帮我看看里面有什么喵", false);
    }

    async function handleUploadImage() {
        if (isWaiting) return;
        if (!window.pywebview || !window.pywebview.api) {
            alert("上传图片需要桌面版（pywebview）环境喵");
            return;
        }
        const path = await window.pywebview.api.pick_image();
        if (!path) return;
        try {
            const r = await fetch("/api/upload", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path: path }),
            });
            const data = await r.json();
            if (!r.ok) { alert(data.detail || "上传失败喵"); return; }
            appendImage(data.url);
            await sendUserMessage("我上传了一张图，图片路径是：" + data.path + "。帮我看看里面有什么喵", false);
        } catch (e) { alert("上传失败喵：" + e); }
    }

    async function handleUploadFile() {
        if (isWaiting) return;
        if (!window.pywebview || !window.pywebview.api) {
            alert("上传文件需要桌面版（pywebview）环境喵");
            return;
        }
        const path = await window.pywebview.api.pick_file();
        if (!path) return;
        try {
            const r = await fetch("/api/upload_file", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path: path }),
            });
            const data = await r.json();
            if (!r.ok) { alert(data.detail || "上传失败喵"); return; }
            appendFile(data.url, data.name);
            await sendUserMessage("我上传了一个文件，路径是：" + data.path + "。帮我看看里面是什么喵", false);
        } catch (e) { alert("上传失败喵：" + e); }
    }

    // ===== 发送消息 =====
    function petEvent(type, payload) {
        fetch("/api/pet/event", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ type: type, ...payload }),
        }).catch(() => {});
    }

    async function sendUserMessage(text, showText) {
        if (isWaiting) return;
        const t = (text || "").trim();
        if (!t) return;
        removeWelcome();
        // 新一轮开始：旧回复不再是最后一条，摘掉它的 🔄 钮（重生成只作用于最后一条）
        if (lastBotEl && lastBotEl.parentNode) {
            const oldRegen = lastBotEl.querySelector(".msg-regen");
            if (oldRegen) oldRegen.remove();
        }
        lastBotEl = null;
        let userEl = null;
        if (showText !== false) userEl = appendMessage("user", t);

        setWaiting(true);
        const typingMsg = appendTyping();
        petEvent("emotion", { mood: "think" });

        try {
            const streamBot = createStreamingBot(typingMsg);
            await fetchSSE("/api/chat_response", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ chatmassage: t, session_id: currentSessionId }),
            }, {
                onMeta(evt) {
                    // user 气泡补上后端分配的消息 id + 删除钮（错误分支不入库，无 id 无按钮）
                    if (userEl && evt.message_ids && evt.message_ids.user) {
                        addRoundButtons(userEl, evt.message_ids.user, false);
                    }
                    // 首条消息自动命名后同步标题（顶栏 + 侧栏）
                    if (evt.session) syncSessionTitle(evt.session);
                },
                onStatus(evt) {
                    // 工具轮：typing 还在就换成状态文字；不在就 toast 兜底
                    const text = (evt && evt.text) || "正在操作电脑喵…";
                    if (typingMsg && typingMsg.parentNode) {
                        const b = typingMsg.querySelector(".msg-bubble");
                        if (b) b.textContent = text;
                    } else {
                        showToast(text);
                    }
                },
                onDelta(evt) { streamBot.delta(evt.text); },
                onDone(evt) {
                    // 来源面板交给 done() 的收尾回调挂（reveal 完成时 lastBotEl 才被赋值）
                    streamBot.done(evt.message_ids && evt.message_ids.assistant, function (el) {
                        if (el && evt.fact_meta && evt.fact_refs) {
                            appendFactPanel(el.querySelector(".msg-body"), evt.message_ids.assistant, evt.fact_refs, evt.fact_meta);
                        }
                    });
                },
                onError(detail) { streamBot.fail(detail); },
            });
        } catch (error) {
            typingMsg.remove();
            appendMessage("bot", "本喵的网络好像出问题了喵…等会儿再试试喵~");
            petEvent("emotion", { mood: "happy" });
            lastBotEl = null;
            refreshMeta();
        } finally {
            setWaiting(false);
        }
    }

    async function handleSend() {
        if (isWaiting) return;
        const text = messageInput.value.trim();
        if (!text) {
            shakeInput();
            return;
        }
        messageInput.value = "";
        messageInput.focus();
        autoResizeInput();
        await sendUserMessage(text);
    }

    // ===== 应用自动发现 =====
    function applyAppsStatus(data) {
        if (!appScanInfo) return;
        if (data.scanned) {
            appScanInfo.textContent = "已发现 " + data.count + " 个应用（快照，装新软件可重扫）";
            if (scanAppsBtn) scanAppsBtn.classList.remove("highlight");
        } else {
            appScanInfo.textContent = "首次使用：先扫描应用";
            if (scanAppsBtn) scanAppsBtn.classList.add("highlight");
        }
    }

    async function loadAppsStatus() {
        try {
            const r = await fetch("/api/apps");
            if (!r.ok) return;
            const data = await r.json();
            applyAppsStatus(data);
            if (!data.scanned) showAppGuideBar();
        } catch (e) { /* 后端未就绪：静默 */ }
    }

    async function scanApps() {
        try {
            const r = await fetch("/api/apps/scan", { method: "POST" });
            if (r.ok) {
                const data = await r.json();
                applyAppsStatus(data);
                showToast("扫描完毕喵，发现 " + data.count + " 个可启动应用");
            }
        } catch (e) { }
    }

    function showAppGuideBar() {
        if (appGuideShown) return;
        appGuideShown = true;
        const bar = document.createElement("div");
        bar.className = "app-guide-bar";
        bar.innerHTML =
            '<span>🔍 主人，本喵还不认识你电脑上的应用~ 扫描一下，以后说「打开微信」我就直接帮你开喵</span>' +
            '<button id="guideScanBtn" class="guide-scan-btn">去扫描</button>' +
            '<span class="guide-close" title="关闭">✕</span>';
        bar.querySelector(".guide-close").addEventListener("click", function () { bar.remove(); });
        bar.querySelector("#guideScanBtn").addEventListener("click", async function () {
            bar.remove();
            await scanApps();
        });
        chatArea.prepend(bar);
    }

    function showToast(text) {
        toast.textContent = text;
        toast.classList.add("show");
        clearTimeout(showToast._timer);
        showToast._timer = setTimeout(function () { toast.classList.remove("show"); }, 2200);
    }

    // ===== 猫娘记忆（L2：查看/清空摘要小本本） =====
    function setMemoryDetailsCollapsed(collapsed) {
        memoryDetailsCollapsed = collapsed;
        if (memoryDetails) memoryDetails.hidden = collapsed;
        if (toggleMemoryBtn) {
            toggleMemoryBtn.textContent = collapsed ? "展开" : "收起";
            toggleMemoryBtn.setAttribute("aria-expanded", String(!collapsed));
        }
    }

    // 记忆卡片按会话查看：下拉框选哪个会话就看哪个（默认当前会话）
    async function populateMemorySessionSelect() {
        if (!memorySessionSelect || !currentSessionId) return;
        try {
            const resp = await fetch("/api/sessions");
            if (!resp.ok) return;
            const data = await resp.json();
            memorySessionSelect.innerHTML = "";
            (data.sessions || []).forEach(function (s) {
                const opt = document.createElement("option");
                opt.value = s.id;
                opt.textContent = s.title || "新对话";
                memorySessionSelect.appendChild(opt);
            });
            memorySessionSelect.value = currentSessionId;
            memoryViewSid = currentSessionId;
        } catch (e) { }
        loadMemory();
        loadContextItems();
    }

    // 切会话/清空后：下拉框和查看目标回到当前会话，再刷新
    function syncMemoryToCurrent() {
        if (memorySessionSelect && memoryViewSid !== currentSessionId) {
            memorySessionSelect.value = currentSessionId;
            memoryViewSid = currentSessionId;
        }
        loadMemory();
        loadContextItems();
    }

    let memoryRequestId = 0;  // 记忆请求序号：快速切会话时防旧请求串内容（app.js优化建议 #5）
    async function loadMemory() {
        if (!memoryList) return;
        const sid = memoryViewSid || currentSessionId;
        if (!sid) return;
        const requestId = ++memoryRequestId;
        try {
            const r = await fetch("/api/sessions/" + encodeURIComponent(sid) + "/summary");
            if (!r.ok || requestId !== memoryRequestId) { if (requestId === memoryRequestId) memoryList.textContent = "加载失败喵"; return; }
            const data = await r.json();
            if (requestId !== memoryRequestId) return;
            const segs = data.summary || [];
            if (!segs.length) {
                memoryList.textContent = "还没有记忆喵，聊得久了猫娘会把重要事情记到小本本上";
                return;
            }
            // 显示层合并成一条：后端分段存（append-only 省 token），这里合成一条展示
            const lines = [];
            segs.forEach(function (s) {
                (s.text || "").split("\n").filter(Boolean).forEach(function (ln) { lines.push(ln); });
            });
            memoryList.innerHTML = "";
            const item = document.createElement("div");
            item.className = "memory-item";
            const num = document.createElement("div");
            num.className = "memory-num";
            num.textContent = "记";
            const text = document.createElement("div");
            text.className = "memory-text";
            text.textContent = lines.join("\n");
            item.appendChild(num);
            item.appendChild(text);
            memoryList.appendChild(item);
        } catch (e) {
            memoryList.textContent = "加载失败喵";
        }
    }

    function setContextItemsCollapsed(collapsed) {
        contextItemsCollapsed = collapsed;
        if (contextItemsDetails) contextItemsDetails.hidden = collapsed;
        if (toggleContextItemsBtn) {
            toggleContextItemsBtn.textContent = collapsed ? "展开" : "收起";
            toggleContextItemsBtn.setAttribute("aria-expanded", String(!collapsed));
        }
    }

    function contextItemPreview(content) {
        const text = (content || "").replace(/\s+/g, " ").trim();
        return text.length > 90 ? text.slice(0, 90) + "..." : text;
    }

    async function loadContextItems() {
        if (!contextItemsList) return;
        const sid = memoryViewSid || currentSessionId;
        if (!sid) return;
        try {
            const r = await fetch("/api/sessions/" + encodeURIComponent(sid) + "/context-items");
            if (!r.ok) { contextItemsList.textContent = "加载失败喵"; return; }
            const data = await r.json();
            const items = data.items || [];
            if (!items.length) { contextItemsList.textContent = "还没有可管理的保留项喵"; return; }
            contextItemsList.innerHTML = "";
            items.forEach(function (item) {
                const row = document.createElement("div");
                row.className = "context-item";
                const badge = document.createElement("span");
                badge.className = "context-policy " + item.policy;
                badge.textContent = item.policy === "pinned" ? "长期" : "暂存";
                const text = document.createElement("span");
                text.className = "context-item-text";
                text.textContent = contextItemPreview(item.content);
                row.appendChild(badge);
                row.appendChild(text);
                if (item.policy === "reference") {
                    const pin = document.createElement("button");
                    pin.className = "context-action";
                    pin.title = "长期保留";
                    pin.textContent = "固定";
                    pin.addEventListener("click", function () { updateContextItem(item.message_id, "pin"); });
                    row.appendChild(pin);
                }
                const drop = document.createElement("button");
                drop.className = "context-action danger";
                drop.title = "停止保留";
                drop.textContent = "移除";
                drop.addEventListener("click", function () { updateContextItem(item.message_id, "drop"); });
                row.appendChild(drop);
                contextItemsList.appendChild(row);
            });
        } catch (e) {
            contextItemsList.textContent = "加载失败喵";
        }
    }

    async function updateContextItem(messageId, action) {
        const sid = memoryViewSid || currentSessionId;
        if (!sid || !messageId) return;
        try {
            const r = await fetch("/api/sessions/" + encodeURIComponent(sid) + "/context-items/" + encodeURIComponent(messageId), {
                method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: action }),
            });
            if (!r.ok) { showToast("操作失败喵"); return; }
            showToast(action === "pin" ? "已长期保留喵" : "已停止保留喵");
            loadContextItems();
        } catch (e) { showToast("操作失败喵"); }
    }

    // ===== 知识库文件（RAG） =====
    function setRagDetailsCollapsed(collapsed) {
        ragDetailsCollapsed = collapsed;
        if (ragDetails) ragDetails.hidden = collapsed;
        if (toggleRagBtn) {
            toggleRagBtn.textContent = collapsed ? "展开" : "收起";
            toggleRagBtn.setAttribute("aria-expanded", String(!collapsed));
        }
    }

    function formatFileSize(size) {
        if (size < 1024) return size + " B";
        if (size < 1024 * 1024) return (size / 1024).toFixed(1) + " KB";
        return (size / (1024 * 1024)).toFixed(1) + " MB";
    }

    async function loadRagFiles() {
        if (!ragFileList) return;
        try {
            const response = await fetch("/api/rag/files");
            if (!response.ok) throw new Error();
            const data = await response.json();
            const files = data.files || [];
            ragStatus.textContent = data.building ? "正在建立索引…" :
                (data.ready ? "索引已就绪" : "索引将在导入后建立");
            ragFileList.innerHTML = "";
            if (!files.length) {
                ragFileList.textContent = "还没有导入资料，可添加 Markdown、文本、Word 或 Excel 文件";
                return;
            }
            files.forEach(function (file) {
                const row = document.createElement("div");
                row.className = "plugin-item";
                const name = document.createElement("span");
                name.className = "plugin-name";
                name.textContent = file.path;
                const detail = document.createElement("span");
                detail.className = "plugin-desc";
                detail.textContent = (file.extension || "").slice(1).toUpperCase() + " · " + formatFileSize(file.size || 0);
                const remove = document.createElement("button");
                remove.className = "plugin-del";
                remove.textContent = "删除";
                remove.addEventListener("click", async function () {
                    if (!confirm("确定从知识库删除「" + file.path + "」吗？删除后将无法被猫娘检索喵")) return;
                    const result = await fetch("/api/rag/files", {
                        method: "DELETE", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ path: file.path }),
                    });
                    const body = await result.json().catch(function () { return {}; });
                    if (!result.ok) { showToast(body.detail || "删除失败喵"); return; }
                    showToast("已从知识库删除，正在更新索引喵");
                    loadRagFiles();
                });
                row.append(name, detail, remove);
                ragFileList.appendChild(row);
            });
        } catch (e) {
            ragStatus.textContent = "知识库状态加载失败";
            ragFileList.textContent = "暂时无法读取知识库文件";
        }
    }

    async function importRagFile() {
        if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.pick_rag_file) {
            alert("导入知识库文件需要桌面版（pywebview）环境喵");
            return;
        }
        const path = await window.pywebview.api.pick_rag_file();
        if (!path) return;
        try {
            const result = await fetch("/api/rag/files", {
                method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: path }),
            });
            const data = await result.json().catch(function () { return {}; });
            if (!result.ok) { showToast(data.detail || "导入失败喵"); return; }
            showToast("已导入「" + data.file.path + "」，正在建立索引喵");
            loadRagFiles();
        } catch (e) { showToast("导入失败喵"); }
    }

    async function rebuildRagIndex() {
        try {
            const result = await fetch("/api/rag/rebuild", { method: "POST" });
            const data = await result.json().catch(function () { return {}; });
            if (!result.ok) { showToast(data.detail || "没有可建立索引的文件喵"); return; }
            showToast("正在重建知识库索引喵");
            loadRagFiles();
        } catch (e) { showToast("重建索引失败喵"); }
    }

    // ===== 插件管理（即插即用） =====
    async function loadPlugins() {
        if (!pluginList) return;
        try {
            const r = await fetch("/api/plugins/reload", { method: "POST" });
            if (r.ok) {
                const data = await r.json();
                renderPlugins(data.plugins || []);
                if (data.new && data.new.length) {
                    showToast("新插件已加载喵：" + data.new.map(p => p.name || p.id).join("、"));
                }
            }
        } catch (e) { }
    }

    function renderPlugins(plugins) {
        pluginList.innerHTML = "";
        if (!plugins.length) {
            pluginList.textContent = "未安装插件（把含 plugin.py 的文件夹拷进插件目录，或点添加）";
            return;
        }
        plugins.forEach(function (p) {
            const row = document.createElement("div");
            row.className = "plugin-item";
            const status = p.status === "loaded"
                ? '<span class="plugin-status ok">已加载</span>'
                : '<span class="plugin-status err">' + escapeHtml(p.error || "加载失败") + "</span>";
            row.innerHTML =
                '<span class="plugin-name">' + escapeHtml(p.name || p.id) + "</span>" +
                '<span class="plugin-desc">' + escapeHtml(p.description || "") +
                "（工具：" + escapeHtml((p.tools || []).join("、") || "无") + "）</span>" +
                status +
                '<button class="plugin-del" data-id="' + escapeHtml(p.id) + '">删除</button>';
            row.querySelector(".plugin-del").addEventListener("click", async function () {
                await fetch("/api/plugins/" + encodeURIComponent(p.id), { method: "DELETE" });
                await loadPlugins();
            });
            pluginList.appendChild(row);
        });
    }

    async function addPlugin() {
        if (!window.pywebview || !window.pywebview.api) {
            alert("添加插件需要桌面版（pywebview）环境喵");
            return;
        }
        const folder = await window.pywebview.api.pick_folder();
        if (!folder) return;
        try {
            const r = await fetch("/api/plugins/add", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path: folder }),
            });
            const data = await r.json();
            if (!r.ok) { alert(data.detail || "添加失败喵"); return; }
            renderPlugins(data.plugins || []);
            if (data.new && data.new.length) {
                showToast("插件已加载，可立即使用喵：" + data.new.map(p => p.name || p.id).join("、"));
            } else {
                showToast("插件已添加喵");
            }
        } catch (e) { }
    }

    // ===== 定点提醒（闹钟） =====
    async function loadAlarms() {
        if (!alarmList) return;
        try {
            const r = await fetch("/api/alarms");
            if (r.ok) {
                const data = await r.json();
                renderAlarms(data.alarms || []);
            }
        } catch (e) { }
    }

    // 已设提醒列表默认收起，按钮显示「查看已设置的提醒（N）」，点击展开/收起
    let _alarmCount = 0;
    let _alarmListExpanded = false;
    function updateAlarmToggle() {
        if (!alarmToggleLabel) return;
        alarmToggleLabel.textContent = _alarmListExpanded
            ? "收起提醒列表 ▾"
            : (_alarmCount > 0 ? "查看已设置的提醒（" + _alarmCount + "）▸" : "查看已设置的提醒 ▸");
    }

    function renderAlarms(alarms) {
        _alarmCount = alarms.length;
        updateAlarmToggle();
        alarmList.innerHTML = "";
        if (!alarms.length) {
            alarmList.textContent = "还没有定点提醒，跟猫娘说『十点半提醒我做事』，或在上方直接添加";
            return;
        }
        alarms.forEach(function (a) {
            const row = document.createElement("div");
            row.className = "plugin-item";
            const repeatTxt = { once: "一次", daily: "每天", weekly: "每周" }[a.repeat] || "一次";
            const when = (a.date && a.repeat === "once") ? a.date.slice(5).replace("-", "/") + " " : "";
            const task = a.action && a.action.tool ? "🔧 自动 " + a.action.tool + " · " :
                (a.goal ? "🎯 自主 " + String(a.goal).replace(/\s+/g, " ").slice(0, 48) + " · " : "");
            row.innerHTML =
                '<span class="plugin-name">' + (task ? (a.goal ? "🎯 " : "🔧 ") : "⏰ ") + escapeHtml(when + a.time) + "</span>" +
                '<span class="plugin-desc">' + escapeHtml(task + repeatTxt + " · " + a.message) + "</span>" +
                '<button class="plugin-del" data-id="' + escapeHtml(a.id) + '">删除</button>';
            row.querySelector(".plugin-del").addEventListener("click", async function () {
                await fetch("/api/alarms/" + encodeURIComponent(a.id), { method: "DELETE" });
                showToast("已删除这条提醒喵");
                await loadAlarms();
            });
            alarmList.appendChild(row);
        });
    }

    async function addAlarm() {
        const time = (alarmTimeInput.value || "").trim();
        const message = (alarmMsgInput.value || "").trim();
        if (!time || !message) {
            showToast("先填时间和提醒内容喵");
            return;
        }
        const repeat = alarmRepeatSelect.value;
        const date = repeat === "once" ? (alarmDateInput.value || "") : "";
        try {
            const r = await fetch("/api/alarms", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ time: time, message: message, repeat: repeat, date: date || null }),
            });
            const data = await r.json();
            if (!r.ok) { showToast(data.detail || "添加失败喵"); return; }
            alarmTimeInput.value = "";
            alarmMsgInput.value = "";
            alarmDateInput.value = "";
            const when = (data.alarm.date) ? data.alarm.date.slice(5).replace("-", "/") + " " : "";
            showToast("已设好 ⏰" + when + data.alarm.time + " 提醒主人「" + data.alarm.message + "」喵");
            await loadAlarms();
        } catch (e) { }
    }

    // 聊天窗实时接收提醒：长轮询 /api/alarms/chat_events（照桌宠模式），
    // 到点 appendMessage 弹一条猫娘消息（无 id=纯展示，不入历史渲染层）
    function startChatAlertPoll() {
        async function loop() {
            try {
                const ctrl = new AbortController();
                const timer = setTimeout(function () { ctrl.abort(); }, 30000);
                const r = await fetch("/api/alarms/chat_events", { signal: ctrl.signal });
                if (r.ok) {
                    const evt = await r.json();
                    if (evt && evt.type === "alarm" && evt.message) {
                        removeWelcome();
                        appendMessage("bot", evt.message);
                        refreshMeta();
                    }
                }
                clearTimeout(timer);
            } catch (e) { /* 超时/断连：歇半秒再拉 */ }
            setTimeout(loop, 500);
        }
        loop();
    }

    // ===== 设置 =====
    function buildThemeSwatches() {
        const container = document.getElementById("themeSwatches");
        if (!container) return;
        THEME_PRESETS.forEach(function (c) {
            const b = document.createElement("button");
            b.className = "swatch";
            b.style.background = c;
            b.addEventListener("click", function () {
                applyTheme(c);
                document.getElementById("themeColorInput").value = c;
                saveSetting("theme_color", c);
            });
            container.appendChild(b);
        });
    }

    // 已渲染消息的头像/时间戳同步（设置晚到或开关切换时刷新，app.js优化建议 #3）
    let _lastAppliedTimestamp = null;  // 上次 applySettings 落地的时间戳开关值；null=还没落地
    function updateRenderedAvatars() {
        chatArea.querySelectorAll(".message.user .msg-avatar img").forEach(function (img) {
            img.src = userAvatarUrl;
        });
        chatArea.querySelectorAll(".message.bot .msg-avatar img").forEach(function (img) {
            img.src = catAvatarUrl;
        });
    }
    function refreshMessageTimestamps() {
        chatArea.querySelectorAll(".message .msg-body").forEach(function (body) {
            let t = body.querySelector(".msg-time");
            if (showTimestamp && !t) {
                t = document.createElement("div");
                t.className = "msg-time";
                t.textContent = nowTime();
                body.insertBefore(t, body.firstChild);
            } else if (!showTimestamp && t) {
                t.remove();
            }
        });
    }

    function applySettings(s) {
        settings = s || {};
        applyTheme(settings.theme_color || "#ec4899");
        brandName.textContent = settings.pet_name || "猫娘";
        userAvatarUrl = "/img/" + (settings.user_avatar || "user.jpg");
        catAvatarUrl = "/img/" + (settings.cat_avatar || "cat.png");
        brandAvatar.src = catAvatarUrl + "?t=" + Date.now();
        document.getElementById("userAvatarPreview").src = userAvatarUrl + "?t=" + Date.now();
        document.getElementById("catAvatarPreview").src = catAvatarUrl + "?t=" + Date.now();
        const fs = parseFloat(settings.font_scale) || 1;
        document.documentElement.style.setProperty("--font-scale", fs);
        document.getElementById("fontScaleInput").value = fs;
        document.getElementById("fontScaleValue").textContent = Math.round(fs * 100) + "%";
        document.getElementById("showPetSwitch").checked = !!settings.show_pet;
        document.getElementById("walkSwitch").checked = !!settings.pet_walk;
        document.getElementById("autostartSwitch").checked = !!settings.autostart;
        document.getElementById("waterSwitch").checked = !!settings.water_reminder;
        document.getElementById("stretchSwitch").checked = !!settings.stretch_reminder;
        document.getElementById("pomodoroSwitch").checked = !!settings.pomodoro;
        document.getElementById("pomodoroWorkInput").value = settings.pomodoro_work || 25;
        document.getElementById("pomodoroBreakInput").value = settings.pomodoro_break || 5;
        document.getElementById("sleepNudgeSwitch").checked = !!settings.sleep_nudge;
        document.getElementById("petNameInput").value = settings.pet_name || "";
        document.getElementById("themeColorInput").value = settings.theme_color || "#ec4899";
        document.getElementById("showTimestampSwitch").checked = !!settings.show_timestamp;
        document.getElementById("soundSwitch").checked = !!settings.notification_sound;
        // 默认开（老配置里没这个字段也当开）
        document.getElementById("searchBrowserSwitch").checked = settings.search_open_browser !== false;
        // 默认关（10-04 拍板：只认「明说搜/查」和「知识库没材料」两条确定性触发）
        renderWebSearchToggle(settings.allow_auto_web_search === true);
        const closeBehaviorSelect = document.getElementById("closeBehaviorSelect");
        if (closeBehaviorSelect) {
            closeBehaviorSelect.value = settings.close_behavior === "exit" ? "exit" : "tray";
        }
        const lanEnabledSwitch = document.getElementById("lanEnabledSwitch");
        if (lanEnabledSwitch) {
            lanEnabledSwitch.checked = !!settings.lan_enabled;
            const lanIpInput = document.getElementById("lanIpInput");
            const lanIp = settings.lan_ip || "";
            lanIpInput.value = lanIp;
            document.getElementById("lanPortInput").value = settings.lan_port || 8800;
            document.getElementById("lanTokenInput").value = settings.lan_token || "";
            document.getElementById("lanPublicOriginInput").value = settings.lan_public_origin || "";
            const addr = settings.lan_enabled && settings.lan_ip ? "手机打开：http://" + settings.lan_ip + ":" + (settings.lan_port || 8800) + "/m（无手机访问 5 分钟后自动关闭）" : "先连接手机热点，再填写电脑获得的 IPv4 地址。";
            document.getElementById("lanAddressHint").textContent = settings.lan_enabled && settings.lan_public_origin ? "手机打开：" + settings.lan_public_origin + "/m（无手机访问 5 分钟后自动关闭）" : addr;
        }
        showTimestamp = !!settings.show_timestamp;
        // 设置落位后同步已渲染消息（可能设置晚于历史消息渲染加载，app.js优化建议 #3）
        updateRenderedAvatars();
        if (showTimestamp !== _lastAppliedTimestamp) {
            refreshMessageTimestamps();
            _lastAppliedTimestamp = showTimestamp;
        }
    }

    async function loadSettings() {
        try {
            const r = await fetch("/api/settings");
            if (r.ok) applySettings(await r.json());
        } catch (e) { /* 后端未就绪，保持默认 */ }
    }

    // 设置保存串行化（app.js优化建议 #2）：saveSetting 并发时排队发，避免后端
    // "读全量-合并-写全量"交错丢写，也避免乱序响应把旧状态刷回界面。
    let settingsSaveQueue = Promise.resolve();
    function saveSetting(key, value) {
        settingsSaveQueue = settingsSaveQueue.then(async function () {
            const payload = {}; payload[key] = value;
            try {
                const r = await fetch("/api/settings", {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
                if (r.ok) applySettings(await r.json());
            } catch (e) { }
        });
        return settingsSaveQueue;
    }

    async function refreshLanIps() {
        const input = document.getElementById("lanIpInput");
        const options = document.getElementById("lanIpOptions");
        if (!input || !options || !window.pywebview || !window.pywebview.api || !window.pywebview.api.list_lan_ips) return;
        try {
            const ips = await window.pywebview.api.list_lan_ips();
            const selected = input.value;
            options.innerHTML = "";
            (ips || []).forEach(function (ip) {
                const option = document.createElement("option");
                option.value = ip;
                options.appendChild(option);
            });
            if (selected) input.value = selected;
            else if (!selected && (ips || []).length === 1) input.value = ips[0];
        } catch (e) { }
    }

    async function changeAvatar(role) {
        if (!window.pywebview || !window.pywebview.api) {
            alert("头像更换需要桌面版（pywebview）环境喵");
            return;
        }
        const path = await window.pywebview.api.pick_image();
        if (!path) return;
        try {
            const r = await fetch("/api/settings/avatar", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ role: role, path: path }),
            });
            const data = await r.json();
            if (data.url) {
                const preview = document.getElementById((role === "user" ? "user" : "cat") + "AvatarPreview");
                preview.src = data.url + "?t=" + Date.now();
                if (role === "user") {
                    userAvatarUrl = data.url;
                } else {
                    catAvatarUrl = data.url;
                    brandAvatar.src = data.url + "?t=" + Date.now();
                }
                updateRenderedAvatars();  // 已渲染的历史消息头像也一起换（app.js优化建议 #3）
            } else {
                alert(data.detail || "换头像失败喵");
            }
        } catch (e) {
            alert("换头像失败喵：" + e);
        }
    }

    // ===== 等待状态 / 输入框 =====
    function setWaiting(waiting) {
        isWaiting = waiting;
        sendBtn.disabled = waiting;
        messageInput.disabled = waiting;
        if (waiting) {
            sendBtn.style.opacity = "0.5";
            sendBtn.style.cursor = "not-allowed";
        } else {
            sendBtn.style.opacity = "";
            sendBtn.style.cursor = "";
            messageInput.focus();
        }
    }

    function shakeInput() {
        messageInput.classList.remove("shake");
        void messageInput.offsetWidth;
        messageInput.classList.add("shake");
        messageInput.focus();
    }

    function autoResizeInput() {
        messageInput.style.height = "auto";
        messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + "px";
    }

    // ===== 事件绑定 =====
    sendBtn.addEventListener("click", handleSend);
    messageInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            handleSend();
        }
    });
    messageInput.addEventListener("input", autoResizeInput);
    if (clearBtn) clearBtn.addEventListener("click", handleClear);
    if (newChatBtn) newChatBtn.addEventListener("click", handleNewSession);
    if (screenshotBtn) screenshotBtn.addEventListener("click", handleScreenshot);
    if (uploadImageBtn) uploadImageBtn.addEventListener("click", handleUploadImage);
    if (uploadFileBtn) uploadFileBtn.addEventListener("click", handleUploadFile);

    // 搜索聊天记录（防抖 + 结果面板 + 点击跳转）
    if (chatSearchInput && chatSearchResults) {
        chatSearchInput.addEventListener("input", function () {
            if (searchTimer) clearTimeout(searchTimer);
            searchTimer = setTimeout(performSearch, 250);
        });
        chatSearchInput.addEventListener("keydown", function (e) {
            if (e.key === "Escape") { resetSearch(); chatSearchInput.blur(); }
            if (e.key === "Enter") { e.preventDefault(); clearTimeout(searchTimer); performSearch(); }
        });
        document.addEventListener("click", function (e) {
            if (!chatSearchInput.contains(e.target) && !chatSearchResults.contains(e.target)) {
                clearSearchPanel();
            }
        });
    }

    // 功能入口：设置→开浮层；提醒/应用/插件/记忆→开浮层并滚到对应分组
    function openSettings() {
        settingsOverlay.classList.add("open");
        loadPlugins();
        loadAppsStatus();
        loadAlarms();
        loadRagFiles();
        populateMemorySessionSelect();
    }
    document.querySelectorAll(".func-item").forEach(function (b) {
        b.addEventListener("click", function () {
            openSettings();
            const target = b.getAttribute("data-scroll");
            if (target) {
                const g = document.getElementById(target);
                if (g) setTimeout(function () {
                    g.scrollIntoView({ behavior: "smooth", block: "start" });
                }, 80);
            }
        });
    });
    settingsCloseBtn.addEventListener("click", function () { settingsOverlay.classList.remove("open"); });
    settingsOverlay.addEventListener("click", function (e) {
        if (e.target === settingsOverlay) settingsOverlay.classList.remove("open");
    });

    // 品牌改名
    nameEditBtn.addEventListener("click", function (e) { e.stopPropagation(); startRename(); });
    brandName.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === "Escape") { e.preventDefault(); brandName.blur(); }
    });
    brandName.addEventListener("blur", endRename);

    // 会话标题改名小窗（顶栏 ✎）
    if (titleEditBtn) {
        titleEditBtn.addEventListener("click", function (e) {
            e.stopPropagation();  // 别冒泡到 document，否则"点 ✎ 开窗"会被外面的关闭handler立刻关掉
            if (titlePopOpen()) closeTitlePop(); else openTitlePop();
        });
    }
    titlePopOk.addEventListener("click", function (e) { e.stopPropagation(); commitTitlePop(); });
    titlePopCancel.addEventListener("click", function (e) { e.stopPropagation(); closeTitlePop(); });
    titlePop.addEventListener("click", function (e) { e.stopPropagation(); });  // 点窗内不关
    titlePopInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); commitTitlePop(); }        // 回车 = 保存
        else if (e.key === "Escape") { e.preventDefault(); closeTitlePop(); }   // Esc = 取消
    });
    // 点窗外任意处 = 取消（不保存）；窗口尺寸变了跟着挪位，别飘到标题外面去
    document.addEventListener("click", function () { if (titlePopOpen()) closeTitlePop(); });
    window.addEventListener("resize", function () { if (titlePopOpen()) placeTitlePop(); });

    // 侧栏折叠
    if (collapseBtn) {
        collapseBtn.addEventListener("click", function () {
            document.body.classList.toggle("sidebar-collapsed");
            setTimeout(function () { updateSidebarFade(); }, 320);
        });
    }

    // 侧栏宽度过渡后补算（resize 只在过渡起点读一次，图标条会卡档）
    const sidebarEl = document.querySelector(".sidebar");
    if (sidebarEl) {
        sidebarEl.addEventListener("transitionend", function (e) {
            if (e.propertyName === "width") { updateSidebarFade(); }
        });
    }
    window.addEventListener("resize", function () { updateSidebarFade(); });

    // 设置面板开关
    document.getElementById("showPetSwitch").addEventListener("change", e => saveSetting("show_pet", e.target.checked));
    document.getElementById("walkSwitch").addEventListener("change", e => saveSetting("pet_walk", e.target.checked));
    document.getElementById("autostartSwitch").addEventListener("change", e => saveSetting("autostart", e.target.checked));
    document.getElementById("waterSwitch").addEventListener("change", e => saveSetting("water_reminder", e.target.checked));
    document.getElementById("stretchSwitch").addEventListener("change", e => saveSetting("stretch_reminder", e.target.checked));
    document.getElementById("pomodoroSwitch").addEventListener("change", e => saveSetting("pomodoro", e.target.checked));
    document.getElementById("sleepNudgeSwitch").addEventListener("change", e => saveSetting("sleep_nudge", e.target.checked));

    // 番茄钟时长（防抖保存）：两个输入共用一次防抖，但各取各的值，
    // 避免"改工作时间时休息时间被同一个 el.value 污染"（app.js优化建议 #1）
    let pomodoroTimer = null;
    function savePomodoroSettings() {
        clearTimeout(pomodoroTimer);
        pomodoroTimer = setTimeout(function () {
            const work = Math.max(1, Math.min(120, parseInt(document.getElementById("pomodoroWorkInput").value, 10) || 25));
            const rest = Math.max(1, Math.min(60, parseInt(document.getElementById("pomodoroBreakInput").value, 10) || 5));
            saveSetting("pomodoro_work", work);
            saveSetting("pomodoro_break", rest);
        }, 400);
    }
    ["pomodoroWorkInput", "pomodoroBreakInput"].forEach(function (id) {
        document.getElementById(id).addEventListener("change", savePomodoroSettings);
    });
    document.getElementById("showTimestampSwitch").addEventListener("change", e => {
        showTimestamp = e.target.checked;
        refreshMessageTimestamps();  // 立即生效，不依赖保存往返
        saveSetting("show_timestamp", e.target.checked);
    });
    document.getElementById("soundSwitch").addEventListener("change", e => saveSetting("notification_sound", e.target.checked));
    document.getElementById("searchBrowserSwitch").addEventListener("change", e => saveSetting("search_open_browser", e.target.checked));

    // 工具栏「联网搜索：on/off」——控的是「猫娘能否自己上网」这个开关，跟设置面板里
    // 那个「搜索时弹出浏览器」是两码事（前者管她自不自主联网，后者管明说搜时弹不弹窗）。
    const webSearchToggle = document.getElementById("webSearchToggle");
    function renderWebSearchToggle(on) {
        if (!webSearchToggle) return;
        webSearchToggle.textContent = "联网搜索：" + (on ? "on" : "off");
        webSearchToggle.title = on
            ? "猫娘能自己上网核验时效性问题（点一下关掉）"
            : "只有你说「搜/查」或知识库没材料时才联网（点一下允许她自己上网）";
        webSearchToggle.classList.toggle("on", !!on);
    }
    if (webSearchToggle) {
        webSearchToggle.addEventListener("click", function () {
            const next = !webSearchToggle.classList.contains("on");
            renderWebSearchToggle(next);        // 立即反馈，不等保存往返
            saveSetting("allow_auto_web_search", next);
        });
    }

    // 点击关闭时的行为（仅隐藏到托盘 / 直接退出）
    const closeBehaviorSelect = document.getElementById("closeBehaviorSelect");
    if (closeBehaviorSelect) {
        closeBehaviorSelect.addEventListener("change", e => saveSetting("close_behavior", e.target.value));
    }

    // 手机接入：IP/端口先保存，开关打开时桌面壳才会实际绑定该私网地址。
    const lanEnabledSwitch = document.getElementById("lanEnabledSwitch");
    if (lanEnabledSwitch) {
        lanEnabledSwitch.addEventListener("change", async function (e) {
            if (!e.target.checked) { saveSetting("lan_enabled", false); return; }
            const ip = document.getElementById("lanIpInput").value;
            if (!ip) {
                e.target.checked = false;
                alert("请先连接手机热点，并选择电脑获得的 IPv4 地址。");
                return;
            }
            await saveSetting("lan_ip", ip);
            saveSetting("lan_enabled", true);
        });
        document.getElementById("lanIpInput").addEventListener("change", e => saveSetting("lan_ip", e.target.value.trim()));
        document.getElementById("lanPortInput").addEventListener("change", e => saveSetting("lan_port", parseInt(e.target.value, 10) || 8800));
        document.getElementById("lanPublicOriginInput").addEventListener("change", e => saveSetting("lan_public_origin", e.target.value.trim()));
        document.getElementById("lanTokenResetBtn").addEventListener("click", () => saveSetting("lan_token", ""));
    }

    // 猫娘名字（设置面板输入，防抖保存；侧栏 ✎ 改名走 endRename）
    let petNameTimer = null;
    document.getElementById("petNameInput").addEventListener("input", e => {
        brandName.textContent = e.target.value || "猫娘";
        clearTimeout(petNameTimer);
        petNameTimer = setTimeout(() => saveSetting("pet_name", e.target.value), 500);
    });

    // 字体大小（实时预览 + change 保存）
    document.getElementById("fontScaleInput").addEventListener("input", e => {
        const v = parseFloat(e.target.value);
        document.documentElement.style.setProperty("--font-scale", v);
        document.getElementById("fontScaleValue").textContent = Math.round(v * 100) + "%";
    });
    document.getElementById("fontScaleInput").addEventListener("change", e => saveSetting("font_scale", parseFloat(e.target.value)));

    // 主题色
    document.getElementById("themeColorInput").addEventListener("input", e => applyTheme(e.target.value));
    document.getElementById("themeColorInput").addEventListener("change", e => saveSetting("theme_color", e.target.value));

    // 应用/插件
    if (scanAppsBtn) scanAppsBtn.addEventListener("click", scanApps);
    if (addPluginBtn) addPluginBtn.addEventListener("click", addPlugin);
    if (reloadPluginsBtn) reloadPluginsBtn.addEventListener("click", loadPlugins);
    if (refreshMemoryBtn) refreshMemoryBtn.addEventListener("click", loadMemory);
    if (toggleMemoryBtn) toggleMemoryBtn.addEventListener("click", function () {
        setMemoryDetailsCollapsed(!memoryDetailsCollapsed);
    });
    if (refreshContextItemsBtn) refreshContextItemsBtn.addEventListener("click", loadContextItems);
    if (toggleContextItemsBtn) toggleContextItemsBtn.addEventListener("click", function () {
        setContextItemsCollapsed(!contextItemsCollapsed);
    });
    if (importRagFileBtn) importRagFileBtn.addEventListener("click", importRagFile);
    if (rebuildRagBtn) rebuildRagBtn.addEventListener("click", rebuildRagIndex);
    if (toggleRagBtn) toggleRagBtn.addEventListener("click", function () {
        setRagDetailsCollapsed(!ragDetailsCollapsed);
    });
    if (memorySessionSelect) memorySessionSelect.addEventListener("change", function () {
        memoryViewSid = memorySessionSelect.value;
        loadMemory();
        loadContextItems();
    });
    if (clearMemoryBtn) clearMemoryBtn.addEventListener("click", async function () {
        const sid = memoryViewSid || currentSessionId;
        if (!sid) return;
        if (!confirm("确定清空这个会话的记忆小本本吗？清空后更早的事它就记不得了喵")) return;
        try {
            await fetch("/api/sessions/" + encodeURIComponent(sid) + "/summary", { method: "DELETE" });
            loadMemory();
        } catch (e) { }
    });

    // 定点提醒
    if (alarmToggleBtn && alarmWrap) alarmToggleBtn.addEventListener("click", function () {
        _alarmListExpanded = !_alarmListExpanded;
        alarmWrap.hidden = !_alarmListExpanded;
        // 展开时若列表还是占位文案（openSettings 已预载，这里兜底），刷新一次
        if (_alarmListExpanded && alarmList.textContent === "加载中…") loadAlarms();
        updateAlarmToggle();
    });
    if (addAlarmBtn) addAlarmBtn.addEventListener("click", addAlarm);
    if (alarmMsgInput) alarmMsgInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); addAlarm(); }
    });
    // 周期选"每天"时日期没意义，禁用日期框
    if (alarmRepeatSelect && alarmDateInput) {
        const syncAlarmDate = function () {
            alarmDateInput.disabled = alarmRepeatSelect.value !== "once";
        };
        alarmRepeatSelect.addEventListener("change", syncAlarmDate);
        syncAlarmDate();
    }

    // 换头像
    document.getElementById("changeUserAvatar").addEventListener("click", () => changeAvatar("user"));
    document.getElementById("changeCatAvatar").addEventListener("click", () => changeAvatar("cat"));

    // ===== 初始化 =====
    buildThemeSwatches();
    applyTheme("#ec4899");
    restoreSessions();
    loadSettings();
    refreshLanIps();
    window.addEventListener("pywebviewready", refreshLanIps);
    loadAppsStatus();
    updateSidebarFade();
    messageInput.focus();
    startChatAlertPoll();
    setInterval(syncRemoteSessions, 4000);
    setInterval(function () {
        if (settingsOverlay.classList.contains("open")) loadSettings();
    }, 15000);
    document.addEventListener("visibilitychange", function () {
        if (!document.hidden) syncRemoteSessions();
    });
})();
