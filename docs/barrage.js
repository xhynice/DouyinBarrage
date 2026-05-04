/**
 * 弹幕展示前端逻辑
 */

const BarrageApp = (function() {
    const TYPE_CONFIG = {
        chat: {
            label: '聊天',
            color: null,
            highlight: false,
            render: (item) => escapeHtml(item.content || '')
        },
        gift: {
            label: '礼物',
            color: '#fe2c55',
            highlight: true,
            render: (item) => {
                const diamond = item.diamond_total > 0 ? ` <span class="diamond">(${item.diamond_total}钻石)</span>` : '';
                return `送出 <span class="gift-name">${escapeHtml(item.gift_name || '')}</span> x${item.gift_count || 1}${diamond}`;
            }
        },
        lucky_bag: {
            label: '福袋',
            color: '#ffd700',
            highlight: true,
            render: (item) => `福袋口令: ${escapeHtml(item.content || '')}`
        },
        member: {
            label: '进场',
            color: null,
            highlight: false,
            render: (item) => {
                const count = item.member_count ? ` (直播间: ${item.member_count}人)` : '';
                return `进入了直播间${count}`;
            }
        },
        social: {
            label: '关注',
            color: null,
            highlight: false,
            render: (item) => {
                const follow = item.follow_count ? ` (第${item.follow_count}个关注)` : '';
                return `${escapeHtml(item.action || '关注了主播')}${follow}`;
            }
        },
        like: {
            label: '点赞',
            color: null,
            highlight: false,
            render: (item) => `点赞了 ${item.count || 0} 次`
        },
        fansclub: {
            label: '粉丝团',
            color: null,
            highlight: false,
            render: (item) => escapeHtml(item.content || '')
        },
        stats: {
            label: '统计',
            color: null,
            highlight: false,
            render: (item) => {
                const parts = [];
                if (item.current) parts.push(`当前: ${item.current}`);
                if (item.total_pv) parts.push(`累计: ${item.total_pv}`);
                if (item.total_user) parts.push(`用户: ${item.total_user}`);
                return parts.join(' | ');
            }
        },
        roomstats: {
            label: '房间统计',
            color: null,
            highlight: false,
            render: (item) => `在线: ${item.total || 0}`
        },
        room: {
            label: '房间',
            color: null,
            highlight: false,
            render: (item) => escapeHtml(item.content || '')
        },
        rank: {
            label: '排行',
            color: null,
            highlight: false,
            render: (item) => `排行榜更新`
        },
        control: {
            label: '控制',
            color: null,
            highlight: false,
            render: (item) => `状态: ${item.status || ''}`
        },
        emoji: {
            label: '表情',
            color: null,
            highlight: false,
            render: (item) => '发送表情'
        }
    };

    const DEFAULT_CONFIG = {
        label: '未知',
        color: null,
        highlight: false,
        render: (item) => JSON.stringify(item)
    };

    const AVATAR_PLACEHOLDER = `data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 28 28%22><rect fill=%22%23ddd%22 width=%2228%22 height=%2228%22/><text x=%2214%22 y=%2214%22 text-anchor=%22middle%22 dy=%22.3em%22 fill=%22%23999%22 font-size=%2210%22>?</text></svg>`;

    const ESCAPE_MAP = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
    };

    let state = {
        index: null,
        currentLiveId: null,
        currentSession: null,
        sessionMeta: null,
        roomSessions: [],
        allData: [],
        roomData: [],
        filteredData: [],
        displayedCount: 0,
        pageSize: 300,
        selectedYear: null,
        filters: {
            types: [],
            search: ''
        },
        searchIndex: {
            session: null,
            room: null
        },
        dateFilter: 'session',
        cachedFilters: null
    };

    function escapeHtml(text) {
        if (!text) return '';
        return String(text).replace(/[&<>"']/g, ch => ESCAPE_MAP[ch]);
    }

    function getGradeClass(level) {
        if (level <= 10) return 'grade-1-10';
        if (level <= 20) return 'grade-11-20';
        if (level <= 30) return 'grade-21-30';
        if (level <= 40) return 'grade-31-40';
        if (level <= 50) return 'grade-41-50';
        if (level <= 60) return 'grade-51-60';
        if (level <= 70) return 'grade-61-70';
        return 'grade-71-75';
    }

    function getFansClubClass(level) {
        if (level <= 5) return 'fans-1-5';
        if (level <= 10) return 'fans-6-10';
        if (level <= 15) return 'fans-11-15';
        if (level <= 20) return 'fans-16-20';
        if (level <= 25) return 'fans-21-25';
        return 'fans-26-30';
    }

    function updateHeader(title, headerTitle, avatarSrc) {
        document.title = title;
        document.getElementById('header-title').textContent = headerTitle;
        const avatarEl = document.getElementById('header-avatar');
        avatarEl.src = avatarSrc;
        avatarEl.onerror = () => { avatarEl.src = AVATAR_PLACEHOLDER; };
        const fav = document.getElementById('favicon');
        fav.href = avatarSrc;
        fav.type = 'image/jpeg';
    }

    function getTypeConfig(type) {
        return TYPE_CONFIG[type] || DEFAULT_CONFIG;
    }

    function parseSessionId(sessionId) {
        const parts = sessionId.split('_');
        if (parts.length >= 2) {
            const dateStr = parts[0];
            const timeStr = parts[1];
            return {
                year: dateStr.substring(0, 4),
                month: parseInt(dateStr.substring(4, 6)),
                day: parseInt(dateStr.substring(6, 8)),
                hour: timeStr.substring(0, 2),
                minute: timeStr.substring(2, 4),
                dateLabel: `${parseInt(dateStr.substring(4, 6))}月${parseInt(dateStr.substring(6, 8))}日`,
                timeLabel: `${timeStr.substring(0, 2)}:${timeStr.substring(2, 4)}`
            };
        }
        return null;
    }

    function renderBarrageItem(item, index) {
        const config = getTypeConfig(item._type);
        const classes = ['barrage-item'];

        if (config.highlight) {
            classes.push('highlight');
            classes.push(`highlight-${item._type}`);
        }

        const content = config.render(item);
        let grade = '';
        if (item.grade) {
            const gradeNum = parseInt(item.grade.replace(/[^\d]/g, '')) || 0;
            const gradeClass = getGradeClass(gradeNum);
            grade = `<span class="grade ${gradeClass}">${gradeNum}</span>`;
        }
        let fansClub = '';
        if (item.fans_club) {
            const fansNum = parseInt(item.fans_club.replace(/.*?Lv(\d+).*/, '$1')) || 0;
            const fansClass = getFansClubClass(fansNum);
            fansClub = `<span class="fans-club ${fansClass}">Lv${fansNum}</span>`;
        }
        const typeLabel = config.label ? `<span class="type-label">${config.label}</span>` : '';

        const metaParts = [typeLabel, grade, fansClub].filter(Boolean);
        const meta = metaParts.length > 0 ? metaParts.join('') : '';

        return `
            <div class="${classes.join(' ')}" data-type="${item._type}" data-index="${index}">
                ${meta}
                <span class="user" data-user="${escapeHtml(item.user_name || '')}">${escapeHtml(item.user_name || '')}</span>
                <div class="content">${content}</div>
                ${item.time ? `<span class="time">${item.time}</span>` : ''}
            </div>
        `;
    }

    async function loadJsonlData(baseUrl, types, extraProps) {
        const results = await Promise.all(types.map(async (type) => {
            const items = [];
            try {
                const res = await fetch(`${baseUrl}/${type}.jsonl`);
                if (!res.ok) {
                    console.warn(`加载 ${type} 数据失败: HTTP ${res.status}`);
                    return items;
                }
                const text = await res.text();
                const lines = text.trim().split('\n');
                for (const line of lines) {
                    if (line) {
                        try {
                            const item = JSON.parse(line);
                            item._type = type;
                            if (extraProps) Object.assign(item, extraProps);
                            items.push(item);
                        } catch (e) {}
                    }
                }
            } catch (error) {
                console.warn(`加载 ${type} 数据失败:`, error);
            }
            return items;
        }));
        const items = results.flat();
        items.sort((a, b) => {
            const timeA = a.time || '';
            const timeB = b.time || '';
            if (!timeA || !timeB) return 0;
            const cmp = timeB.localeCompare(timeA);
            if (cmp === 0) return 0;
            const toSec = t => {
                const parts = t.split(':');
                return parts.length >= 2 ? parseInt(parts[0]) * 3600 + parseInt(parts[1]) * 60 + (parseInt(parts[2]) || 0) : 0;
            };
            const secA = toSec(timeA);
            const secB = toSec(timeB);
            const diff = Math.abs(secB - secA);
            if (diff > 43200) {
                return secB > secA ? -1 : 1;
            }
            return cmp;
        });
        return items;
    }

    async function loadIndex() {
        try {
            const res = await fetch('data/barrage/index.json');
            if (!res.ok) {
                throw new Error(`HTTP ${res.status}`);
            }
            state.index = await res.json();
            state.index.live_rooms.sort((a, b) => (b.latest_session || '').localeCompare(a.latest_session || ''));
            renderLiveRoomList();
        } catch (error) {
            console.error('加载索引失败:', error);
            document.getElementById('empty-state').innerHTML = `
                <div class="empty-icon">❌</div>
                <p>加载数据失败，请先运行构建脚本</p>
                <p style="color: var(--text-secondary); margin-top: 10px;">python scripts/build_barrage.py</p>
            `;
        }
    }

    function initRoomSwitcher() {
        const switchBtn = document.getElementById('room-switch-btn');
        const rooms = state.index.live_rooms || [];

        if (!switchBtn || rooms.length <= 1) {
            if (switchBtn) switchBtn.style.display = 'none';
            return;
        }

        switchBtn.style.display = 'inline-flex';
        switchBtn.replaceWith(switchBtn.cloneNode(true));

        document.getElementById('room-switch-btn').addEventListener('click', () => {
            showRoomSwitchModal();
        });

        document.getElementById('room-switch-close').addEventListener('click', () => {
            hideRoomSwitchModal();
        });

        document.getElementById('room-switch-overlay').addEventListener('click', () => {
            hideRoomSwitchModal();
        });
    }

    function showRoomSwitchModal() {
        const modal = document.getElementById('room-switch-modal');
        const list = document.getElementById('room-list');
        const rooms = state.index.live_rooms || [];

        list.innerHTML = rooms.map(r => {
            const isActive = r.live_id === state.currentLiveId;
            const displayName = r.anchor_name || r.live_id;
            const totalStats = r.total_stats || {};
            const totalCount = Object.values(totalStats).reduce((sum, v) => sum + (v || 0), 0);
            return `
                <div class="room-list-item ${isActive ? 'active' : ''}" data-live-id="${r.live_id}">
                    <img class="room-list-avatar" src="data/barrage/${r.live_id}/avatar.jpg" alt="${escapeHtml(displayName)}" onerror="this.src='${getPlaceholderSvg()}';">
                    <div class="room-list-info">
                        <div class="room-list-name">${escapeHtml(displayName)}</div>
                        <div class="room-list-stats">
                            <span>${r.session_count || 0} 会话</span>
                            <span>${totalCount.toLocaleString()} 条弹幕</span>
                        </div>
                    </div>
                    ${isActive ? '<div class="room-list-check">✓</div>' : ''}
                </div>
            `;
        }).join('');

        list.querySelectorAll('.room-list-item').forEach(el => {
            el.addEventListener('click', () => {
                const liveId = el.dataset.liveId;
                if (liveId !== state.currentLiveId) {
                    selectLiveRoom(liveId);
                }
                hideRoomSwitchModal();
            });
        });

        modal.style.display = 'block';
        setTimeout(() => modal.classList.add('active'), 10);
    }

    function hideRoomSwitchModal() {
        const modal = document.getElementById('room-switch-modal');
        modal.classList.remove('active');
        setTimeout(() => modal.style.display = 'none', 200);
    }

    function getPlaceholderSvg() {
        return "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 48 48'><rect fill='%23ddd' width='48' height='48'/><text x='24' y='24' text-anchor='middle' dy='.3em' fill='%23999' font-size='16'>?</text></svg>";
    }

    function renderLiveRoomList() {
        const rooms = state.index.live_rooms || [];

        if (rooms.length === 0) return;

        initRoomSwitcher();

        const firstRoom = rooms[0];
        selectLiveRoom(firstRoom.live_id);
    }

    function resetCascadingSelects() {
        state.selectedYear = null;
        ['year-select', 'datetime-select'].forEach(id => {
            const el = document.getElementById(id);
            el.classList.add('hidden');
            el.querySelector('.custom-select-trigger span').textContent =
                id === 'year-select' ? '选择年份' : '选择日期时间';
        });
    }

    async function selectLiveRoom(liveId) {
        if (state.currentLiveId && state.currentLiveId !== liveId) {
            showLoading(true, '切换直播间...');
            document.getElementById('barrage-list').classList.add('hidden');
            document.getElementById('type-stats').classList.add('hidden');
            document.getElementById('empty-state').classList.add('hidden');
            document.getElementById('type-filters').classList.add('hidden');
            document.getElementById('search-input').value = '';
            document.getElementById('search-input').disabled = true;
        }

        state.currentLiveId = liveId;
        state.roomData = [];
        state.searchIndex = { session: null, room: null };
        state.dateFilter = '';
        state.cachedFilters = null;
        resetCascadingSelects();
        resetDateFilter();

        try {
            const res = await fetch(`data/barrage/${liveId}/index.json`);
            if (!res.ok) {
                throw new Error(`HTTP ${res.status}`);
            }
            const data = await res.json();
            const sessions = data.sessions || [];
            state.roomSessions = sessions;

            const displayName = data.anchor_name || liveId;
            const avatarUrl = `data/barrage/${liveId}/avatar.jpg`;
            const title = data.room_title ? ` | ${data.room_title}` : '';

            updateHeader(`${displayName} - 弹幕记录`, displayName, avatarUrl);
            document.getElementById('subtitle').textContent = `${liveId}${title}`;

            document.getElementById('search-box').classList.remove('hidden');
            showLoading(false);

            if (sessions.length === 0) {
                showEmpty('该直播间暂无会话数据');
                return;
            }

            renderYearList(sessions);
            renderDateFilterOptions(sessions);

            const roomInfo = (state.index.live_rooms || []).find(r => r.live_id === liveId);
            const latestSession = roomInfo?.latest_session;
            if (latestSession) {
                const parsed = parseSessionId(latestSession);
                if (parsed) {
                    selectYear(parsed.year);
                    selectDatetime(latestSession);
                    return;
                }
            }

            const years = [...new Set(sessions.map(s => parseSessionId(s.session_id)?.year).filter(Boolean))];
            if (years.length === 1) {
                selectYear(years[0]);
            }
        } catch (error) {
            console.error('加载直播间索引失败:', error);
            showLoading(false);
            showEmpty('加载直播间数据失败');
        }
    }

    function renderYearList(sessions) {
        const container = document.getElementById('year-options');
        const yearMap = new Map();

        sessions.forEach(s => {
            const parsed = parseSessionId(s.session_id);
            if (parsed) {
                if (!yearMap.has(parsed.year)) {
                    yearMap.set(parsed.year, 0);
                }
                yearMap.set(parsed.year, yearMap.get(parsed.year) + (s.total || 0));
            }
        });

        const years = [...yearMap.keys()].sort().reverse();

        container.innerHTML = years.map(year => `
            <div class="custom-select-option" data-value="${year}" data-label="${year}年">
                ${year}年
                <span class="option-meta">${yearMap.get(year)} 条</span>
            </div>
        `).join('');

        document.getElementById('year-select').classList.remove('hidden');
    }

    function renderDateFilterOptions(sessions) {
        const container = document.getElementById('date-filter-options');
        const yearMap = new Map();

        sessions.forEach(s => {
            const parsed = parseSessionId(s.session_id);
            if (parsed) {
                const yearKey = parsed.year;
                const monthKey = `${parsed.year}-${String(parsed.month).padStart(2, '0')}`;
                if (!yearMap.has(yearKey)) yearMap.set(yearKey, { count: 0, months: new Map() });
                const yearData = yearMap.get(yearKey);
                yearData.count += (s.total || 0);
                if (!yearData.months.has(monthKey)) yearData.months.set(monthKey, 0);
                yearData.months.set(monthKey, yearData.months.get(monthKey) + (s.total || 0));
            }
        });

        let html = '<div class="custom-select-option active" data-value="session">当前会话</div>';

        const years = [...yearMap.keys()].sort().reverse();
        for (const year of years) {
            const yearData = yearMap.get(year);
            html += `<div class="custom-select-option" data-value="${year}">${year}年 <span class="option-meta">${yearData.count} 条</span></div>`;
            const months = [...yearData.months.keys()].sort().reverse();
            for (const month of months) {
                const monthNum = parseInt(month.split('-')[1]);
                html += `<div class="custom-select-option" data-value="${month}">&nbsp;&nbsp;${monthNum}月 <span class="option-meta">${yearData.months.get(month)} 条</span></div>`;
            }
        }

        container.innerHTML = html;
    }

    function resetDateFilter() {
        state.dateFilter = 'session';
        const wrapper = document.getElementById('date-filter-wrapper');
        wrapper.classList.remove('hidden');
        wrapper.querySelector('.custom-select-trigger span').textContent = '当前会话';
        wrapper.querySelector('.custom-select-trigger').dataset.value = 'session';
    }

    function selectYear(year) {
        state.selectedYear = year;
        document.querySelector('#year-select .custom-select-trigger span').textContent = `${year}年`;

        const datetimeSelect = document.getElementById('datetime-select');
        datetimeSelect.classList.add('hidden');
        datetimeSelect.querySelector('.custom-select-trigger span').textContent = '选择日期时间';

        const filtered = state.roomSessions.filter(s => {
            const parsed = parseSessionId(s.session_id);
            return parsed && parsed.year === year;
        });

        renderDatetimeList(filtered);

        if (filtered.length === 1) {
            selectDatetime(filtered[0].session_id);
        }
    }

    function renderDatetimeList(sessions) {
        const container = document.getElementById('datetime-options');

        const sorted = [...sessions].sort((a, b) => (b.session_id || '').localeCompare(a.session_id || ''));

        container.innerHTML = sorted.map(session => {
            const parsed = parseSessionId(session.session_id);
            const label = parsed ? `${parsed.dateLabel} ${parsed.timeLabel}` : session.session_id;
            return `
                <div class="custom-select-option" data-value="${session.session_id}" data-label="${label}">
                    ${label}
                    <span class="option-meta">${session.total || 0} 条</span>
                </div>
            `;
        }).join('');

        document.getElementById('datetime-select').classList.remove('hidden');
    }

    function selectDatetime(sessionId) {
        const parsed = parseSessionId(sessionId);
        const label = parsed ? `${parsed.dateLabel} ${parsed.timeLabel}` : sessionId;
        document.querySelector('#datetime-select .custom-select-trigger span').textContent = label;
        selectSession(sessionId);
    }

    async function selectSession(sessionId) {
        state.currentSession = sessionId;

        showLoading(true);
        hideEmpty();
        hideBarrageList();

        try {
            const metaRes = await fetch(`data/barrage/${state.currentLiveId}/${sessionId}/meta.json`);
            if (!metaRes.ok) {
                throw new Error(`HTTP ${metaRes.status}`);
            }
            state.sessionMeta = await metaRes.json();

            renderTypeFilters(state.sessionMeta.available_types || []);
            updateStats(state.sessionMeta.stats || {});

            await loadSessionData();

            document.getElementById('search-input').disabled = false;
            document.getElementById('search-btn').disabled = false;
            document.getElementById('type-filters').classList.remove('hidden');
        } catch (error) {
            console.error('加载会话数据失败:', error);
            showEmpty('加载会话数据失败');
        } finally {
            showLoading(false);
        }
    }

    function buildSearchIndex(data, scope) {
        const indexKey = scope || (state.dateFilter === 'session' ? 'session' : 'room');
        const index = new Array(data.length);
        for (let i = 0; i < data.length; i++) {
            const item = data[i];
            index[i] = {
                userName: (item.user_name || '').toLowerCase(),
                content: (item.content || '').toLowerCase(),
                giftName: (item.gift_name || '').toLowerCase()
            };
        }
        state.searchIndex[indexKey] = index;
    }

    async function loadSessionData() {
        const types = state.sessionMeta.available_types || [];
        showLoading(true, '加载会话数据...');
        state.allData = await loadJsonlData(
            `data/barrage/${state.currentLiveId}/${state.currentSession}`,
            types
        );
        buildSearchIndex(state.allData, 'session');
        applyFilters();
    }

    function renderTypeFilters(availableTypes) {
        const container = document.getElementById('type-filters');

        const allBtn = `<button class="type-filter-btn active" data-type="">全部</button>`;
        const typeBtns = availableTypes.map(type => {
            const config = getTypeConfig(type);
            return `<button class="type-filter-btn" data-type="${type}">${config.label}</button>`;
        }).join('');

        container.innerHTML = allBtn + typeBtns;
    }

    function formatNumber(num) {
        if (num >= 10000) {
            return `${(num / 10000).toFixed(1)}<small class="pv-unit">万</small>`;
        }
        return num;
    }

    function updateStats(stats) {
        document.getElementById('stats').classList.remove('hidden');
        
        const chat = stats.chat || 0;
        document.getElementById('stat-chat').innerHTML = formatNumber(chat);

        const social = stats.social || 0;
        document.getElementById('stat-social').innerHTML = formatNumber(social);

        const meta = state.sessionMeta || {};
        const giftEl = document.getElementById('stat-gift');
        if (meta.gift_diamond) {
            const gift = meta.gift_diamond;
            giftEl.innerHTML = `${formatNumber(gift)}<small class="pv-unit">音浪</small>`;
        } else {
            giftEl.textContent = 0;
        }

        const pvEl = document.getElementById('stat-pv');
        if (meta.total_pv) {
            const pv = typeof meta.total_pv === 'string' ? parseInt(meta.total_pv) || 0 : meta.total_pv;
            if (pv >= 10000) {
                pvEl.innerHTML = `${(pv / 10000).toFixed(1)}<small class="pv-unit">万</small>`;
            } else {
                pvEl.textContent = pv;
            }
        } else {
            pvEl.textContent = '-';
        }
    }

    async function loadRoomData() {
        if (state.roomData.length > 0) return;

        const sessions = state.roomSessions;
        const total = sessions.length;
        const batchSize = 5;
        const allItems = [];

        for (let i = 0; i < total; i += batchSize) {
            const batch = sessions.slice(i, i + batchSize);
            showLoading(true, `加载全直播间数据... ${Math.min(i + batchSize, total)}/${total}`);
            const batchResults = await Promise.all(batch.map(session => {
                const types = session.available_types || [];
                return loadJsonlData(
                    `data/barrage/${state.currentLiveId}/${session.session_id}`,
                    types,
                    { _session: session.session_id }
                );
            }));
            for (const items of batchResults) {
                allItems.push(...items);
            }
        }

        allItems.sort((a, b) => {
            const sessA = a._session || '';
            const sessB = b._session || '';
            const dateA = sessA.split('_')[0] || '';
            const dateB = sessB.split('_')[0] || '';
            if (dateA !== dateB) {
                return dateB.localeCompare(dateA);
            }
            const timeA = a.time || '';
            const timeB = b.time || '';
            if (!timeA || !timeB) return 0;
            const cmp = timeB.localeCompare(timeA);
            if (cmp === 0) return 0;
            const toSec = t => {
                const parts = t.split(':');
                return parts.length >= 2 ? parseInt(parts[0]) * 3600 + parseInt(parts[1]) * 60 + (parseInt(parts[2]) || 0) : 0;
            };
            const secA = toSec(timeA);
            const secB = toSec(timeB);
            const diff = Math.abs(secB - secA);
            if (diff > 43200) {
                return secB > secA ? -1 : 1;
            }
            return cmp;
        });
        state.roomData = allItems;
        buildSearchIndex(state.roomData);
    }

    async function applyFilters() {
        const isSession = state.dateFilter === 'session';
        let data = isSession ? state.allData : state.roomData;
        const indexKey = isSession ? 'session' : 'room';

        if (!isSession && state.roomData.length === 0 && (state.filters.search || state.dateFilter)) {
            await loadRoomData();
            data = state.roomData;
            showLoading(false);
        }

        if (state.filters.search) {
            const raw = state.filters.search;
            const searchIndex = state.searchIndex[indexKey];
            if (raw.startsWith('@')) {
                const query = raw.substring(1).toLowerCase();
                if (query) {
                    if (searchIndex && searchIndex.length === data.length) {
                        data = data.filter((_, i) => searchIndex[i].userName.includes(query));
                    } else {
                        data = data.filter(item => (item.user_name || '').toLowerCase().includes(query));
                    }
                }
            } else {
                const query = raw.toLowerCase();
                if (searchIndex && searchIndex.length === data.length) {
                    data = data.filter((_, i) => searchIndex[i].content.includes(query) || searchIndex[i].giftName.includes(query));
                } else {
                    data = data.filter(item => {
                        const content = (item.content || '').toLowerCase();
                        const giftName = (item.gift_name || '').toLowerCase();
                        return content.includes(query) || giftName.includes(query);
                    });
                }
            }
        }

        if (state.dateFilter && !isSession) {
            data = data.filter(item => {
                const session = item._session || '';
                if (state.dateFilter.includes('-')) {
                    const [fy, fm] = state.dateFilter.split('-');
                    const sy = session.substring(0, 4);
                    const sm = session.substring(4, 6);
                    return sy === fy && sm === fm;
                }
                const sy = session.substring(0, 4);
                return sy === state.dateFilter;
            });
        }

        if (state.filters.types.length > 0) {
            data = data.filter(item => state.filters.types.includes(item._type));
        }

        state.filteredData = data;
        state.displayedCount = 0;

        updateFilterInfo();
        renderTypeStats();
        renderBarrageList();
    }

    function renderTypeStats() {
        const el = document.getElementById('type-stats');
        const type = state.filters.types.length === 1 ? state.filters.types[0] : null;

        if (!type || !['chat', 'gift', 'like', 'lucky_bag'].includes(type)) {
            el.classList.add('hidden');
            el.innerHTML = '';
            return;
        }

        const rankings = (state.sessionMeta || {}).rankings || {};
        const r = rankings[type];
        if (!r) {
            el.classList.add('hidden');
            return;
        }

        let html = '';

        if (type === 'chat') {
            const totalChat = r.top_users.reduce((sum, u) => sum + u.count, 0);
            html = `<div class="ts-section"><div class="ts-title">💬 发弹幕最多</div><div class="ts-list">${r.top_users.map((u, i) => `<div class="ts-rank"><span class="ts-medal">${i + 1}</span><span class="ts-name">${escapeHtml(u.name)}</span><span class="ts-value">${u.count} 条</span></div>`).join('')}</div></div>`;
            if (r.top_at && r.top_at.length > 0) {
                const totalAt = r.top_at.reduce((sum, u) => sum + u.count, 0);
                html += `<div class="ts-section"><div class="ts-title">📢 @人最多</div><div class="ts-list">${r.top_at.map((u, i) => `<div class="ts-rank"><span class="ts-medal">${i + 1}</span><span class="ts-name">${escapeHtml(u.name)}</span><span class="ts-value">${u.count} 次</span></div>`).join('')}</div></div>`;
            }
            html += `<div class="ts-summary">Top6 共 ${totalChat} 条弹幕</div>`;
        }

        if (type === 'gift') {
            const totalGiftDiamond = r.top_users.reduce((sum, u) => sum + u.diamond, 0);
            html = `<div class="ts-section"><div class="ts-title">🎁 送礼最多</div><div class="ts-list">${r.top_users.map((u, i) => `<div class="ts-rank"><span class="ts-medal">${i + 1}</span><span class="ts-name">${escapeHtml(u.name)}</span><span class="ts-value">${u.diamond} 音浪</span>${u.max_gift ? `<span class="ts-gift">${escapeHtml(u.max_gift)}</span><span class="ts-value">${u.max_gift_diamond} 音浪</span>` : ''}</div>`).join('')}</div></div>`;
            html += `<div class="ts-summary">Top6 共 💎 ${totalGiftDiamond} 音浪</div>`;
        }

        if (type === 'like') {
            const totalLikes = r.top_users.reduce((sum, u) => sum + u.count, 0);
            html = `<div class="ts-section"><div class="ts-title">👍 点赞最多</div><div class="ts-list">${r.top_users.map((u, i) => `<div class="ts-rank"><span class="ts-medal">${i + 1}</span><span class="ts-name">${escapeHtml(u.name)}</span><span class="ts-value">${u.count} 次</span></div>`).join('')}</div></div>`;
            html += `<div class="ts-summary">Top6 共 ${totalLikes} 次点赞</div>`;
        }

        if (type === 'lucky_bag') {
            const totalLb = r.top_users.reduce((sum, u) => sum + u.count, 0);
            html = `<div class="ts-section"><div class="ts-title">🎯 参与最多</div><div class="ts-list">${r.top_users.map((u, i) => `<div class="ts-rank"><span class="ts-medal">${i + 1}</span><span class="ts-name">${escapeHtml(u.name)}</span><span class="ts-value">${u.count} 次</span></div>`).join('')}</div></div>`;
            html += `<div class="ts-summary">Top6 共 ${totalLb} 次参与</div>`;
        }

        el.innerHTML = html;
        el.classList.remove('hidden');
    }

    function updateFilterInfo() {
        const filterInfo = document.getElementById('filter-info');
        const filterText = document.getElementById('filter-text');
        const clearBtn = document.getElementById('clear-filter-btn');

        const hasDateFilter = state.dateFilter && state.dateFilter !== 'session';
        const hasFilter = state.filters.types.length > 0 || state.filters.search || hasDateFilter;

        if (hasFilter) {
            filterInfo.classList.remove('hidden');
            const parts = [];

            if (hasDateFilter) {
                if (state.dateFilter.includes('-')) {
                    const [y, m] = state.dateFilter.split('-');
                    parts.push(`时间: ${y}年${parseInt(m)}月`);
                } else {
                    parts.push(`时间: ${state.dateFilter}年`);
                }
            }

            if (state.filters.types.length > 0) {
                const labels = state.filters.types.map(t => getTypeConfig(t).label);
                parts.push(`类型: ${labels.join(', ')}`);
            }

            if (state.filters.search) {
                const scopeText = state.dateFilter === 'session' ? '当前会话' : '全直播间';
                parts.push(`搜索(${scopeText}): "${state.filters.search}"`);
            }

            parts.push(`共 ${state.filteredData.length} 条`);
            filterText.textContent = parts.join(' | ');
            clearBtn.classList.remove('hidden');
        } else {
            filterInfo.classList.add('hidden');
        }
    }

    function renderBarrageList() {
        const container = document.getElementById('barrage-list');
        const loadMoreEl = document.getElementById('load-more');

        if (state.filteredData.length === 0) {
            showEmpty('暂无数据');
            container.classList.add('hidden');
            loadMoreEl.classList.add('hidden');
            return;
        }

        hideEmpty();
        container.classList.remove('hidden');

        const items = state.filteredData.slice(0, state.displayedCount + state.pageSize);
        state.displayedCount = items.length;

        container.innerHTML = items.map((item, i) => renderBarrageItem(item, i)).join('');

        loadMoreEl.classList.toggle('hidden', state.displayedCount >= state.filteredData.length);

        container.scrollTop = 0;
    }

    function loadMore() {
        const container = document.getElementById('barrage-list');
        const loadMoreEl = document.getElementById('load-more');
        const items = state.filteredData.slice(state.displayedCount, state.displayedCount + state.pageSize);

        if (items.length === 0) {
            loadMoreEl.classList.add('hidden');
            return;
        }

        const btn = loadMoreEl.querySelector('button');
        const originalText = btn.textContent;
        btn.textContent = '加载中...';
        btn.disabled = true;

        requestAnimationFrame(() => {
            const html = items.map((item, i) => renderBarrageItem(item, state.displayedCount + i)).join('');
            container.insertAdjacentHTML('beforeend', html);
            state.displayedCount += items.length;

            btn.textContent = `已加载 ${items.length} 条`;
            setTimeout(() => {
                btn.textContent = originalText;
                btn.disabled = false;
            }, 1000);

            loadMoreEl.classList.toggle('hidden', state.displayedCount >= state.filteredData.length);
        });
    }

    function showLoading(show, text) {
        const el = document.getElementById('loading');
        el.classList.toggle('hidden', !show);
        const p = el.querySelector('p');
        if (show) {
            p.textContent = text || '加载中...';
        } else {
            p.textContent = '加载中...';
        }
    }

    function showEmpty(message) {
        const empty = document.getElementById('empty-state');
        empty.innerHTML = `<div class="empty-icon">📭</div><p>${escapeHtml(message)}</p>`;
        empty.classList.remove('hidden');
    }

    function hideEmpty() {
        document.getElementById('empty-state').classList.add('hidden');
    }

    function hideBarrageList() {
        document.getElementById('barrage-list').classList.add('hidden');
        document.getElementById('load-more').classList.add('hidden');
    }

    function jumpToBarrage(targetItem) {
        const isSession = state.dateFilter === 'session';
        const sourceData = isSession ? state.allData : state.roomData;
        const targetIndex = sourceData.indexOf(targetItem);
        if (targetIndex === -1) return;

        state.cachedFilters = {
            search: state.filters.search,
            types: [...state.filters.types],
            dateFilter: state.dateFilter
        };

        state.filters.types = [];
        state.filters.search = '';
        state.dateFilter = isSession ? 'session' : '';
        state.filteredData = sourceData;

        const dateFilterTrigger = document.querySelector('#date-filter-wrapper .custom-select-trigger');
        dateFilterTrigger.dataset.value = state.dateFilter;
        dateFilterTrigger.querySelector('span').textContent = isSession ? '当前会话' : '全部时间';

        document.querySelectorAll('#date-filter-options .custom-select-option').forEach(opt => {
            opt.classList.toggle('active', opt.dataset.value === state.dateFilter);
        });

        document.querySelectorAll('.type-filter-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.type === '');
        });

        const container = document.getElementById('barrage-list');
        const loadMoreEl = document.getElementById('load-more');

        hideEmpty();
        container.classList.remove('hidden');

        const contextAfter = state.pageSize - 10;
        const endIndex = Math.min(sourceData.length, targetIndex + contextAfter);
        const items = sourceData.slice(0, endIndex);

        container.innerHTML = items.map((item, i) => renderBarrageItem(item, i)).join('');
        state.displayedCount = items.length;

        loadMoreEl.classList.toggle('hidden', state.displayedCount >= sourceData.length);

        const targetEl = container.querySelector(`[data-index="${targetIndex}"]`);
        if (targetEl) {
            targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
            targetEl.classList.add('jump-highlight');
            setTimeout(() => targetEl.classList.remove('jump-highlight'), 3000);
        }

        updateFilterInfo();
    }

    function clearFilters() {
        state.filters.types = [];
        state.filters.search = '';
        state.dateFilter = 'session';
        state.cachedFilters = null;
        document.getElementById('search-input').value = '';

        document.querySelectorAll('.type-filter-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.type === '');
        });

        const dateFilterTrigger = document.querySelector('#date-filter-wrapper .custom-select-trigger');
        dateFilterTrigger.dataset.value = 'session';
        dateFilterTrigger.querySelector('span').textContent = '当前会话';

        document.querySelectorAll('#date-filter-options .custom-select-option').forEach(opt => {
            opt.classList.toggle('active', opt.dataset.value === 'session');
        });

        applyFilters();
    }

    function closeAllCustomSelects() {
        document.querySelectorAll('.custom-select').forEach(select => {
            select.classList.remove('open', 'options-right');
        });
    }

    function initCustomSelects() {
        document.querySelectorAll('.custom-select').forEach(select => {
            const trigger = select.querySelector('.custom-select-trigger');
            const optionsContainer = select.querySelector('.custom-select-options');

            trigger.addEventListener('click', (e) => {
                e.stopPropagation();
                const isOpen = select.classList.contains('open');
                closeAllCustomSelects();
                if (!isOpen) {
                    const rect = trigger.getBoundingClientRect();
                    const isRightSide = rect.right > window.innerWidth - 200;
                    select.classList.toggle('options-right', isRightSide);
                    select.classList.add('open');
                }
            });

            if (optionsContainer) {
                optionsContainer.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const option = e.target.closest('.custom-select-option');
                    if (!option || option.classList.contains('disabled')) return;

                    const value = option.dataset.value;

                    select.querySelectorAll('.custom-select-option').forEach(opt => opt.classList.remove('active'));
                    option.classList.add('active');

                    select.classList.remove('open');

                    handleSelectChange(select.id, value, option);
                });
            }
        });
    }

    function handleSelectChange(selectId, value, option) {
        if (selectId === 'year-select') {
            selectYear(value);
            return;
        }

        if (selectId === 'datetime-select') {
            selectDatetime(value);
            return;
        }

        if (selectId === 'date-filter-wrapper') {
            const label = option.textContent.trim();
            document.querySelector('#date-filter-wrapper .custom-select-trigger span').textContent = label;
            document.querySelector('#date-filter-wrapper .custom-select-trigger').dataset.value = value;
            state.dateFilter = value;
            applyFilters();
            return;
        }
    }

    function bindEvents() {
        initCustomSelects();

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                closeAllCustomSelects();
            }
        });

        document.addEventListener('click', (e) => {
            if (!e.target.closest('.custom-select')) {
                closeAllCustomSelects();
            }
        });

        document.getElementById('barrage-list').addEventListener('click', function(e) {
            const userEl = e.target.closest('.user');
            if (userEl) {
                e.stopPropagation();
                const userName = userEl.dataset.user;
                if (userName) {
                    const searchInput = document.getElementById('search-input');
                    searchInput.value = '@' + userName;
                    state.filters.search = '@' + userName;
                    state.cachedFilters = null;
                    applyFilters();
                }
                return;
            }

            const item = e.target.closest('.barrage-item');
            if (!item) return;
            if (!state.filters.search && state.filters.types.length === 0) return;

            const index = parseInt(item.dataset.index);
            const targetItem = state.filteredData[index];
            if (targetItem) {
                jumpToBarrage(targetItem);
            }
        });

        document.getElementById('type-filters').addEventListener('click', function(e) {
            const btn = e.target.closest('.type-filter-btn');
            if (!btn) return;

            const type = btn.dataset.type;

            if (type === '') {
                state.filters.types = [];
                document.querySelectorAll('.type-filter-btn').forEach(b => {
                    b.classList.toggle('active', b.dataset.type === '');
                });
            } else {
                const isActive = btn.classList.contains('active');
                document.querySelectorAll('.type-filter-btn').forEach(b => b.classList.remove('active'));

                if (isActive) {
                    state.filters.types = [];
                    document.querySelector('.type-filter-btn[data-type=""]').classList.add('active');
                } else {
                    btn.classList.add('active');
                    state.filters.types = [type];
                }
            }

            applyFilters();
        });

        document.getElementById('search-input').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                document.getElementById('search-btn').click();
            }
        });

        document.getElementById('search-input').addEventListener('input', function() {
            if (state.cachedFilters) {
                state.cachedFilters = null;
            }
        });

        document.getElementById('search-btn').addEventListener('click', function() {
            const inputValue = document.getElementById('search-input').value.trim();
            if (!inputValue && !state.cachedFilters) return;

            if (state.cachedFilters && inputValue === state.cachedFilters.search) {
                state.filters.search = state.cachedFilters.search;
                state.filters.types = [...state.cachedFilters.types];
                state.dateFilter = state.cachedFilters.dateFilter;
                state.cachedFilters = null;

                const dateFilterTrigger = document.querySelector('#date-filter-wrapper .custom-select-trigger');
                dateFilterTrigger.dataset.value = state.dateFilter;
                const dateFilterLabel = state.dateFilter === 'session' ? '当前会话' : 
                    (state.dateFilter.includes('-') ? state.dateFilter : state.dateFilter + '年');
                dateFilterTrigger.querySelector('span').textContent = dateFilterLabel;

                document.querySelectorAll('.type-filter-btn').forEach(btn => {
                    const t = btn.dataset.type;
                    if (t === '') {
                        btn.classList.toggle('active', state.filters.types.length === 0);
                    } else {
                        btn.classList.toggle('active', state.filters.types.includes(t));
                    }
                });

                applyFilters();
            } else {
                state.cachedFilters = null;
                state.filters.search = inputValue;
                applyFilters();
            }
        });

        document.getElementById('clear-filter-btn').addEventListener('click', clearFilters);

        document.getElementById('load-more-btn').addEventListener('click', loadMore);
    }

    return {
        init: async function() {
            bindEvents();
            await loadIndex();
        }
    };
})();

document.addEventListener('DOMContentLoaded', () => BarrageApp.init());
