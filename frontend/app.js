const apiRoot = '/api/v1';
const pageSize = 100;
const overviewPageSize = 50;
const tokenTableColumns = ['chain', 'tokenIdentity', 'address', 'marketCap', 'marketUpdated', 'liquidity', 'holders', 'reachMc', 'reachHigh', 'growthCurrent', 'growthPeak', 'buyers', 'socials', 'firstSeen', 'favorite'];
const pageTotalKeys = {
    tokens: 'tokens', followed: 'followed_traders',
    favorites: 'favorites', graduated: 'graduations', risk: 'risk_findings', dismissed: 'dismissed_tokens',
};
const searchablePages = new Set(['tokens', 'followed', 'favorites', 'graduated', 'risk', 'dismissed']);
const searchCountEndpoints = {
    tokens: '/tokens/count', followed: '/followed-traders/count', favorites: '/favorites/count', graduated: '/graduated-tokens/count',
    risk: '/risk-analysis/count', dismissed: '/dismissed-tokens/count',
};
let followedMonitorStatus = { state: 'disabled', message: null };
const pageDefinitions = {
    overview: {
        title: 'Overview', breadcrumb: 'Overview',
        description: 'Recently tracked contracts and their latest market performance.',
        eyebrow: 'LIVE WORKSPACE', kicker: 'RECENTLY TRACKED', tableTitle: 'Latest tracked tokens',
        endpoint: '/tokens/recent-bought', emptyTitle: 'No recently bought tokens',
        emptyDescription: 'Tokens purchased by followed profiles will appear here.',
        columns: ['select', ...tokenTableColumns],
    },
    tokens: {
        title: 'The Tokens', breadcrumb: 'The Tokens',
        description: 'Every contract currently in your monitoring list.',
        eyebrow: 'TOKEN UNIVERSE', kicker: 'MONITORING', tableTitle: 'Tracked contracts',
        endpoint: '/tokens/', emptyTitle: 'Your watchlist is clear',
        emptyDescription: 'Add a contract to start tracking it across the available features.',
        columns: ['select', ...tokenTableColumns],
    },
    followed: {
        title: 'Trader watchlist', breadcrumb: 'Traders',
        description: 'Potential token growth from each trader’s first qualifying buy, not realized returns. Failure rate requires 3 observations spanning 7 days.',
        eyebrow: 'FOLLOWED USERS', kicker: 'WATCHLIST', tableTitle: 'Followed traders',
        endpoint: '/followed-traders/', emptyTitle: 'No traders on your list',
        emptyDescription: 'Add a username to keep your watchlist in one place.',
        columns: ['select', 'username', 'signal', 'score', 'totalPicks', 'success10x', 'success5x', 'success2x', 'averageGrowth', 'medianGrowth', 'bestPick', 'failureRate', 'platform', 'tracking', 'synced', 'syncStatus'],
    },
    favorites: {
        title: 'Favorites', breadcrumb: 'Favorites',
        description: 'Tokens saved for closer review, with their values at the time.',
        eyebrow: 'SAVED RESEARCH', kicker: 'BOOKMARKED TOKENS', tableTitle: 'Favorite tokens',
        endpoint: '/favorites/', emptyTitle: 'No favorites saved',
        emptyDescription: 'Tokens marked as favorites will be collected here for comparison.',
        columns: ['select', ...tokenTableColumns],
    },
    graduated: {
        title: 'Graduated tokens', breadcrumb: 'Graduated',
        description: 'Tokens that crossed their configured market-cap threshold.',
        eyebrow: 'MILESTONE TRACKING', kicker: 'THRESHOLD EVENTS', tableTitle: 'Graduation history',
        endpoint: '/graduated-tokens/', emptyTitle: 'No graduations yet',
        emptyDescription: 'Tokens that reach their graduation threshold will be recorded here.',
        columns: ['select', ...tokenTableColumns],
    },
    risk: {
        title: 'Risk signals', breadcrumb: 'Risk',
        description: 'Review detected risk findings, severity, and confirmation status.',
        eyebrow: 'DUE DILIGENCE', kicker: 'FINDINGS', tableTitle: 'Risk findings',
        endpoint: '/risk-analysis/', emptyTitle: 'No risk findings recorded',
        emptyDescription: 'New findings from risk analysis will appear here for review.',
        columns: ['select', ...tokenTableColumns, 'date', 'delete'],
    },
    ai: {
        title: 'AI discovery', breadcrumb: 'AI discovery',
        description: 'Rank and review active tokens from The Tokens using tracked market and trader activity.',
        eyebrow: 'RESEARCH SYSTEMS', kicker: 'TRACKED TOKEN ANALYSIS', tableTitle: 'Tracked token candidates',
        endpoint: '/ai-discovery/', emptyTitle: 'No discovery results yet',
        emptyDescription: 'Run an analysis to rank active tokens from The Tokens.',
        columns: ['aiRank', 'aiTokenIdentity', 'favorite', 'chain', 'aiRecommendation', 'aiScore', 'marketCap', 'liquidity', 'buyers', 'holders', 'aiSummary', 'aiStrengths', 'aiRisks'],
    },
    dismissed: {
        title: 'Dismissed tokens', breadcrumb: 'Dismissed',
        description: 'A record of tokens removed from active discovery review.',
        eyebrow: 'REVIEW HISTORY', kicker: 'DISMISSED', tableTitle: 'Dismissal history',
        endpoint: '/dismissed-tokens/', emptyTitle: 'Nothing dismissed',
        emptyDescription: 'Dismissed tokens and their review status will appear here.',
        columns: ['tokenIdentity', 'address', 'reason', 'restored', 'date'],
    },
};

const columnDefinitions = {
    select: { label: 'Select', value: () => '', action: 'select' },
    tokenIdentity: { label: 'Token', title: 'Token name and ticker', value: (row) => row.name || row.ticker || 'Unknown token', action: 'tokenIdentity' },
    address: { label: 'Contract', title: 'Token contract address', value: (row) => row.contract_address || row.normalized_address || '—', mono: true, action: 'contract' },
    chain: { label: 'Chain', title: 'Blockchain network', value: (row) => chainLabel(row.chain_name) || (row.chain_id ? `Chain ${row.chain_id}` : '—'), icon: 'chain' },
    source: { label: 'Source', value: (row) => row.provider_name || row.source || '—' },
    confidence: { label: 'Confidence', value: (row) => percent(row.confidence), badge: true, tone: 'blue' },
    marketCap: { label: 'MC', title: 'Market cap (MC)', value: (row) => compactMoney(row.market_cap ?? row.initial_market_cap ?? row.market_cap_at_favorite ?? row.market_cap_at_graduation) },
    marketUpdated: { label: 'Updated', title: 'Data updated', value: (row) => dateTime(row.observed_at) },
    price: { label: 'Price', value: (row) => money(row.price ?? row.price_at_favorite, true) },
    liquidity: { label: 'Liq.', title: 'Liquidity', value: (row) => compactMoney(row.liquidity) },
    holders: { label: 'FH', title: 'Fomo holders (FH)', value: (row) => row.fomo_holder_count === null || row.fomo_holder_count === undefined ? '—' : Number(row.fomo_holder_count).toLocaleString() },
    reachMc: { label: 'RMC', title: 'Reached market cap (RMC)', value: (row) => compactMoney(row.reach_market_cap) },
    reachHigh: { label: 'RH', title: 'All-time high since tracking (RH)', value: (row) => compactMoney(row.reached_high_market_cap) },
    growthCurrent: {
        label: 'RMC→MC',
        title: 'Growth from reached market cap to current market cap',
        value: (row) => growthLabel(row.reach_market_cap, row.market_cap),
        growthPercent: (row) => growthPercent(row.reach_market_cap, row.market_cap),
        action: 'growth',
    },
    growthPeak: {
        label: 'RMC→RH',
        title: 'Growth from reached market cap to all-time high',
        value: (row) => growthLabel(row.reach_market_cap, row.reached_high_market_cap),
        growthPercent: (row) => growthPercent(row.reach_market_cap, row.reached_high_market_cap),
        action: 'growth',
    },
    buyers: { label: 'FU', title: 'Followed-user buyers (FU)', value: (row) => String(row.buyer_count ?? 0), action: 'buyers' },
    socials: { label: 'Links', title: 'Social links', value: (row) => row.social_links || [], action: 'socials' },
    favorite: { label: 'Fav.', title: 'Favorite', value: (row) => row.is_favorite ? '★' : '☆', action: 'favorite' },
    delete: { label: 'Dismiss', title: 'Move token to Dismissed', value: () => '×', action: 'delete' },
    volume: { label: 'Volume', value: (row) => money(row.volume) },
    provider: { label: 'Provider', value: (row) => row.provider_name || row.source || '—' },
    date: { label: 'Recorded', value: (row) => dateTime(row.event_time || row.discovered_at || row.observed_at || row.graduated_at || row.detected_at || row.favorited_at || row.dismissed_at) },
    firstSeen: { label: 'Added', title: 'Reached app', value: (row) => dateTime(row.first_seen_at) },
    username: { label: 'Username', value: (row) => row.username || '—', primary: true, action: 'profile' },
    signal: { label: 'Track record', title: 'Category uses qualifying picks, 10× success rate, and median growth', value: (row) => traderSignal(row).label },
    score: { label: 'Score', title: '0–100 score: 2× rate 35%, 5× rate 25%, 10× rate 20%, median growth 15%, failure proxy 5%; adjusted for sample size', value: (row) => row.performance_score === null ? '—' : `${Number(row.performance_score).toFixed(1)} / 100` },
    totalPicks: { label: 'Picks', title: 'Qualifying trader-token relationships with RMC below $50,000', value: (row) => String(row.total_picks ?? 0) },
    success10x: { label: '10×', title: 'Share of qualifying picks reaching at least 10× RMC', value: (row) => performancePercent(row.success_rate_10x) },
    success5x: { label: '5×', title: 'Share of qualifying picks reaching at least 5× RMC', value: (row) => performancePercent(row.success_rate_5x) },
    success2x: { label: '2×', title: 'Share of qualifying picks reaching at least 2× RMC', value: (row) => performancePercent(row.success_rate_2x) },
    averageGrowth: { label: 'Avg.', title: 'Average maximum observed growth multiple from RMC to RH', value: (row) => growthMultiple(row.average_growth) },
    medianGrowth: { label: 'Median', title: 'Median maximum observed growth multiple from RMC to RH', value: (row) => growthMultiple(row.median_growth) },
    bestPick: { label: 'Best pick', title: 'Highest maximum observed growth multiple', value: (row) => row.best_pick ? `${row.best_pick.label} · ${growthMultiple(row.best_pick.growth_multiple)}` : '—' },
    failureRate: { label: 'Failure', title: 'Failure proxy among picks observed at least 3 times across 7 days', value: (row) => performancePercent(row.failure_rate) },
    aiRank: { label: 'Rank', value: (row) => `#${row.rank}` },
    aiTokenIdentity: { label: 'Token', value: (row) => row.name || row.ticker || 'Unknown token', action: 'aiTokenIdentity' },
    aiRecommendation: { label: 'Assessment', value: (row) => row.recommendation || 'Review' },
    aiScore: { label: 'Score', title: 'Combined tracked-signal and local-model score from 0 to 100', value: (row) => `${Number(row.score ?? 0).toFixed(1)} / 100` },
    aiSummary: { label: 'Analysis', value: (row) => row.summary || 'No summary available' },
    aiStrengths: { label: 'Signals', value: (row) => row.strengths?.join(' · ') || '—' },
    aiRisks: { label: 'Risks', value: (row) => row.risks?.join(' · ') || '—' },
    platform: { label: 'Platform ID', value: (row) => row.platform_identifier || 'Fomo Family' },
    tracking: { label: 'Tracking', value: (row) => row.tracking_enabled ? 'Enabled' : 'Paused', badge: true, toneFrom: (row) => row.tracking_enabled ? 'positive' : 'disabled' },
    synced: { label: 'Last synced', value: (row) => dateTime(row.last_synced_at) },
    syncStatus: {
        label: 'Sync status',
        value: (row) => row.sync_error ? 'Needs attention' : row.last_synced_at ? 'Synced'
            : followedMonitorStatus.current_username?.toLowerCase() === row.username?.toLowerCase() ? 'Syncing'
                : ({
                    disabled: 'Monitor off', starting: 'Connecting', connecting: 'Connecting', syncing: 'Queued',
                    waiting: 'Queued', unavailable: 'Unavailable', stopped: 'Stopped',
                }[followedMonitorStatus.state] || 'Queued'),
        badge: true,
        toneFrom: (row) => row.sync_error || followedMonitorStatus.state === 'unavailable'
            ? 'critical' : row.last_synced_at ? 'positive' : 'pending',
    },
    threshold: { label: 'Threshold', value: (row) => money(row.threshold_usd) },
    peak: { label: 'Reached MC', value: (row) => money(row.highest_observed_market_cap) },
    severity: { label: 'Severity', value: (row) => row.severity || 'Unknown', badge: true, toneFrom: (row) => String(row.severity || '').toLowerCase() },
    category: { label: 'Category', value: (row) => row.category || '—' },
    summary: { label: 'Finding', value: (row) => row.summary || '—' },
    confirmed: { label: 'Confirmed', value: (row) => row.confirmed ? 'Confirmed' : 'Unconfirmed', badge: true, toneFrom: (row) => row.confirmed ? 'confirmed' : 'pending' },
    status: { label: 'Status', value: (row) => row.is_graduated ? 'Graduated' : 'Tracking', badge: true, toneFrom: (row) => row.is_graduated ? 'positive' : 'tracking' },
    reason: { label: 'Reason', value: (row) => row.reason || 'Not specified' },
    restored: { label: 'Review state', value: (row) => row.restored ? 'Restored' : 'Dismissed', badge: true, toneFrom: (row) => row.restored ? 'positive' : 'pending' },
};

const elements = {
    nav: document.getElementById('primary-nav'), title: document.getElementById('page-title'),
    breadcrumb: document.getElementById('breadcrumb-current'), description: document.getElementById('page-description'),
    eyebrow: document.getElementById('page-eyebrow'), kicker: document.getElementById('section-kicker'),
    tableTitle: document.getElementById('table-title'), tableHead: document.getElementById('table-head'),
    tableBody: document.getElementById('table-body'), tableFrame: document.getElementById('table-frame'),
    empty: document.getElementById('empty-state'), emptyTitle: document.getElementById('empty-title'),
    emptyDescription: document.getElementById('empty-description'), error: document.getElementById('error-state'),
    errorMessage: document.getElementById('error-message'), count: document.getElementById('result-count'),
    metrics: document.getElementById('metric-grid'), updated: document.getElementById('updated-at'),
    search: document.getElementById('view-search'), searchControl: document.getElementById('search-control'), primaryAction: document.getElementById('primary-action'),
    actionLabel: document.getElementById('primary-action-label'), dialog: document.getElementById('entry-dialog'),
    aiRunButton: document.getElementById('ai-run-button'),
    tokenForm: document.getElementById('token-form'), traderForm: document.getElementById('trader-form'),
    toast: document.getElementById('toast'), connection: document.getElementById('connection-label'),
    connectionDot: document.querySelector('.connection-dot'), refresh: document.getElementById('refresh-button'),
    pagination: document.getElementById('pagination-controls'),
    deleteSelected: document.getElementById('delete-selected-tokens'),
    footnote: document.getElementById('table-footnote'),
    traderSort: document.getElementById('trader-sort'), traderSortControl: document.getElementById('trader-sort-control'),
};

const bulkDeleteRoutes = {
    overview: { endpoint: '/tokens/bulk-delete', bodyKey: 'token_ids', countKey: 'tokens', itemLabel: 'token' },
    tokens: { endpoint: '/tokens/bulk-delete', bodyKey: 'token_ids', countKey: 'tokens', itemLabel: 'token' },
    followed: { endpoint: '/followed-traders/bulk-delete', bodyKey: 'trader_ids', countKey: 'followed_traders', itemLabel: 'trader' },
    favorites: { endpoint: '/favorites/bulk-delete', bodyKey: 'favorite_ids', countKey: 'favorites', itemLabel: 'favorite' },
    graduated: { endpoint: '/tokens/bulk-delete', bodyKey: 'token_ids', countKey: 'graduations', itemLabel: 'token' },
    risk: { endpoint: '/dismissed-tokens/bulk-dismiss', bodyKey: 'token_ids', countKey: 'risk_findings', itemLabel: 'token', action: 'dismiss' },
    dismissed: { endpoint: '/dismissed-tokens/bulk-delete', bodyKey: 'dismissed_ids', countKey: 'dismissed_tokens', itemLabel: 'dismissed record' },
};

let activePage = 'overview';
let activeRows = [];
let toastTimer;
let loadSequence = 0;
let searchTimer;
let deleteRowsInProgress = false;
let activeResultCount;
const selectedRowsByPage = new Map();
const pageCache = new Map();
const pageIndexes = new Map();
let workspaceCounts = {};
let traderSort = 'score';

function pageSizeFor(page) {
    if (page === 'overview') return overviewPageSize;
    return page === 'dismissed' ? 10 : pageSize;
}

function selectedRowIds(page = activePage) {
    if (!selectedRowsByPage.has(page)) selectedRowsByPage.set(page, new Set());
    return selectedRowsByPage.get(page);
}

function rowSelectionId(row, page = activePage) {
    return ['graduated', 'risk'].includes(page) ? row.token_id : row.id;
}

function chainLabel(value) {
    if (!value) return '';
    const normalized = value.toLowerCase().replaceAll('_', ' ');
    const labels = {
        bnb: 'BNB Chain', 'bnb chain': 'BNB Chain', 'robinhood chain': 'Robinhood Chain',
        'fomo network 1399811149': 'Solana', 'fomo network 8453': 'Base', 'fomo network 4663': 'Robinhood Chain',
        'fomo network 1': 'Ethereum', 'fomo network 56': 'BNB Chain',
    };
    return labels[normalized] || value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function money(value, precise = false) {
    if (value === null || value === undefined || value === '') return '—';
    const amount = Number(value);
    if (!Number.isFinite(amount)) return String(value);
    return new Intl.NumberFormat('en-US', {
        style: 'currency', currency: 'USD',
        minimumFractionDigits: precise && amount > 0 && amount < 1 ? 4 : 0,
        maximumFractionDigits: precise && amount > 0 && amount < 1 ? 8 : 2,
    }).format(amount);
}

function compactMoney(value) {
    if (value === null || value === undefined || value === '') return '—';
    const amount = Number(value);
    if (!Number.isFinite(amount)) return String(value);
    const absolute = Math.abs(amount);
    const [scale, suffix] = absolute >= 1_000_000_000 ? [1_000_000_000, 'b']
        : absolute >= 1_000_000 ? [1_000_000, 'm']
            : absolute >= 1_000 ? [1_000, 'k'] : [1, ''];
    if (!suffix) return money(amount);
    return `${amount < 0 ? '-$' : '$'}${(absolute / scale).toLocaleString('en-US', { maximumFractionDigits: 2 })}${suffix}`;
}

function growthPercent(baseline, value) {
    if (baseline === null || baseline === undefined || baseline === '' || value === null || value === undefined || value === '') return null;
    const initial = Number(baseline);
    const current = Number(value);
    if (!Number.isFinite(initial) || initial <= 0 || !Number.isFinite(current)) return null;
    const percentChange = (current / initial) - 1;
    return percentChange * 100;
}

function growthLabel(baseline, value) {
    const percentChange = growthPercent(baseline, value);
    if (percentChange === null) return '—';
    return `${percentChange > 0 ? '+' : ''}${percentChange.toFixed(1)}%`;
}

function percent(value) {
    if (value === null || value === undefined || value === '') return '—';
    const amount = Number(value);
    if (!Number.isFinite(amount)) return String(value);
    return `${Math.round(amount <= 1 ? amount * 100 : amount)}%`;
}

function performancePercent(value) {
    return value === null || value === undefined ? '—' : `${Number(value).toFixed(1)}%`;
}

function growthMultiple(value) {
    return value === null || value === undefined ? '—' : `${Number(value).toFixed(2)}×`;
}

function fomoTokenUrl(row) {
    if (row.fomo_url) return row.fomo_url;
    if (!row.contract_address) return null;
    const chain = String(row.chain_name || '').toLowerCase().replaceAll('_', '-');
    const slugs = { 'bnb-chain': 'bnb', bsc: 'bnb', 'robinhood-chain': 'robinhood' };
    return `https://fomo.family/tokens/${encodeURIComponent(slugs[chain] || chain)}/${encodeURIComponent(row.contract_address)}`;
}

function traderSignal(row) {
    const picks = Number(row.total_picks || 0);
    const tenXRate = Number(row.success_rate_10x || 0);
    const median = Number(row.median_growth || 0);
    if (picks < 3) return { key: 'early', label: 'Early sample', tone: 'early' };
    if (picks >= 5 && tenXRate >= 20 && median >= 2) return { key: 'leader', label: 'Strong record', tone: 'leader' };
    if (tenXRate >= 10 || median >= 1.5) return { key: 'promising', label: 'Promising', tone: 'promising' };
    return { key: 'watch', label: 'Developing', tone: 'watch' };
}

function dateTime(value) {
    if (!value) return 'Not yet';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(date);
}

async function fetchJson(path, options = {}) {
    const response = await fetch(`${apiRoot}${path}`, { cache: 'no-store', ...options });
    if (!response.ok) {
        const detail = await response.text();
        let message = detail || 'Request failed';
        try {
            const payload = JSON.parse(detail);
            message = payload.detail || payload.message || message;
        } catch {
            // Keep non-JSON error bodies readable.
        }
        throw new Error(`${response.status}: ${message}`);
    }
    if (response.status === 204) return null;
    return response.json();
}

async function copyText(value) {
    try {
        await navigator.clipboard.writeText(value);
        return;
    } catch {
        const field = document.createElement('textarea');
        field.value = value;
        field.setAttribute('readonly', '');
        field.style.position = 'fixed';
        field.style.opacity = '0';
        document.body.append(field);
        field.select();
        const copied = document.execCommand('copy');
        field.remove();
        if (!copied) throw new Error('Clipboard access is unavailable');
    }
}

function setMetrics(metrics) {
    elements.metrics.replaceChildren();
    elements.metrics.classList.toggle('metric-grid--three', metrics.length === 3);
    metrics.forEach((metric, index) => {
        const card = document.createElement('article');
        card.className = 'metric-card';
        card.dataset.tone = metric.tone || 'green';
        const label = document.createElement('div');
        label.className = 'metric-label';
        label.append(document.createTextNode(metric.label));
        const number = document.createElement('span');
        number.className = 'metric-index';
        number.textContent = String(index + 1).padStart(2, '0');
        label.append(number);
        const value = document.createElement('div');
        value.className = 'metric-value';
        value.textContent = metric.value;
        const caption = document.createElement('div');
        caption.className = 'metric-caption';
        caption.textContent = metric.caption || 'Current workspace';
        card.append(label, value, caption);
        elements.metrics.append(card);
    });
}

function defaultMetrics(rows, page, totalOverride) {
    const total = rows.length;
    const exactTotals = {
        tokens: workspaceCounts.tokens,
        followed: workspaceCounts.followed_traders,
        favorites: workspaceCounts.favorites,
        graduated: workspaceCounts.graduations,
        risk: workspaceCounts.risk_findings,
        dismissed: workspaceCounts.dismissed_tokens,
    };
    const exactTotal = totalOverride ?? exactTotals[page] ?? total;
    if (page === 'tokens') {
        return [
            { label: 'Tracked tokens', value: String(exactTotal), caption: 'Contracts in your watchlist' },
            { label: 'Graduated', value: String(workspaceCounts.graduations ?? rows.filter((row) => row.is_graduated).length), tone: 'lime', caption: 'Reached a configured threshold' },
            { label: 'Chains', value: String(new Set(rows.map((row) => row.chain_id).filter(Boolean)).size), tone: 'blue', caption: 'Represented in this list' },
            { label: 'Updated', value: 'Live', tone: 'orange', caption: 'Latest API response' },
        ];
    }
    if (page === 'followed') {
        return [
            { label: 'Watchlist', value: String(exactTotal), caption: 'Followed traders' },
            { label: 'Tracking enabled', value: String(rows.filter((row) => row.tracking_enabled).length), tone: 'lime', caption: 'Active tracking records' },
            { label: 'Synced', value: String(rows.filter((row) => row.last_synced_at).length), tone: 'blue', caption: 'Have a sync timestamp' },
            { label: 'Mode', value: 'Manual', tone: 'orange', caption: 'Provider integration dependent' },
        ];
    }
    if (page === 'risk') {
        return [
            { label: 'Findings', value: String(total), caption: 'Latest risk events' },
            { label: 'High severity', value: String(rows.filter((row) => ['high', 'critical'].includes(String(row.severity).toLowerCase())).length), tone: 'orange', caption: 'Prioritize for review' },
            { label: 'Confirmed', value: String(rows.filter((row) => row.confirmed).length), tone: 'blue', caption: 'Confirmed findings' },
            { label: 'Categories', value: String(new Set(rows.map((row) => row.category).filter(Boolean)).size), tone: 'lime', caption: 'Distinct risk categories' },
        ];
    }
    if (page === 'dismissed') {
        return [
            { label: 'Dismissed tokens', value: String(exactTotal), caption: 'Excluded from active tracking' },
            { label: 'Recent', value: String(rows.length), tone: 'blue', caption: 'Most recently dismissed entries' },
            { label: 'Sort order', value: 'Newest', tone: 'lime', caption: 'Latest dismissals first' },
            { label: 'Search', value: 'Ready', tone: 'orange', caption: 'Filter by name, ticker, or address' },
        ];
    }
    const labels = {
        favorites: ['Favorites', 'Saved for review'],
        graduated: ['Graduations', 'Passed configured threshold'],
        dismissed: ['Dismissed', 'Removed from active review'],
    };
    const details = labels[page] || ['Records', 'Latest API response'];
    return [
        { label: details[0], value: String(exactTotal), caption: details[1] },
        { label: 'This view', value: 'Live', tone: 'blue', caption: 'Loaded directly from the API' },
        { label: 'Sort order', value: 'Recent', tone: 'lime', caption: 'Newest records first' },
        { label: 'Filter', value: 'Ready', tone: 'orange', caption: 'Search the visible records' },
    ];
}

function updateNavigationCounts(counts) {
    const values = {
        overview: null,
        tokens: counts.tokens,
        followed: counts.followed_traders,
        favorites: counts.favorites,
        graduated: counts.graduations,
        risk: counts.risk_findings,
        ai: null,
        dismissed: counts.dismissed_tokens,
    };
    document.querySelectorAll('[data-count]').forEach((element) => {
        const count = values[element.dataset.count];
        element.textContent = count === null || count === undefined ? '' : Number(count).toLocaleString();
    });
}

function decrementWorkspaceCount(key, amount = 1) {
    if (workspaceCounts[key] === undefined) return;
    workspaceCounts[key] = Math.max(0, Number(workspaceCounts[key]) - amount);
    updateNavigationCounts(workspaceCounts);
}

function setTableColumns(keys) {
    const row = document.createElement('tr');
    keys.forEach((key) => {
        const column = columnDefinitions[key];
        if (!column) return;
        const heading = document.createElement('th');
        heading.scope = 'col';
        heading.dataset.column = key;
        if (column.title) {
            heading.title = column.title;
            heading.setAttribute('aria-label', column.title);
        }
        if (key === 'select') {
            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.className = 'selection-checkbox';
            checkbox.dataset.action = 'select-page';
            checkbox.setAttribute('aria-label', 'Select all rows on this page');
            heading.append(checkbox);
            row.append(heading);
            return;
        }
        heading.textContent = column.label;
        row.append(heading);
    });
    elements.tableHead.replaceChildren(row);
}

function renderTable(rows, keys) {
    const query = elements.search.value.trim().toLowerCase();
    const filteredRows = rows.filter((row) => !query || Object.values(row).some((value) => String(value ?? '').toLowerCase().includes(query)));
    elements.tableBody.replaceChildren();
    filteredRows.forEach((row, rowIndex) => {
        const tableRow = document.createElement('tr');
        tableRow.style.animationDelay = `${Math.min(rowIndex, 8) * 18}ms`;
        keys.forEach((key) => {
            const column = columnDefinitions[key];
            if (!column) return;
            const cell = document.createElement('td');
            cell.dataset.column = key;
            const value = column.value(row);
            if (column.action === 'select') {
                const rowId = Number(rowSelectionId(row));
                const checkbox = document.createElement('input');
                checkbox.type = 'checkbox';
                checkbox.className = 'selection-checkbox';
                checkbox.dataset.action = 'select-row';
                checkbox.dataset.rowId = String(rowId);
                checkbox.checked = selectedRowIds().has(rowId);
                checkbox.setAttribute('aria-label', `Select ${row.username || row.ticker || row.name || `record #${rowId}`}`);
                cell.append(checkbox);
            } else if (column.action === 'aiTokenIdentity') {
                const identity = document.createElement('div');
                identity.className = 'token-identity';
                const avatar = document.createElement('span');
                avatar.className = 'token-avatar';
                avatar.setAttribute('aria-hidden', 'true');
                avatar.textContent = String(row.ticker || value || '?').slice(0, 1).toUpperCase();
                identity.append(avatar);
                const copy = document.createElement('span');
                copy.className = 'token-identity-copy';
                const name = document.createElement('strong');
                const link = fomoTokenUrl(row) ? document.createElement('a') : null;
                if (link) {
                    link.className = 'ai-token-link';
                    link.href = fomoTokenUrl(row);
                    link.target = '_blank';
                    link.rel = 'noopener noreferrer';
                    link.title = 'Open token on Fomo Family';
                    link.textContent = value;
                    name.append(link);
                } else {
                    name.textContent = value;
                }
                const ticker = document.createElement('small');
                ticker.textContent = row.ticker || 'Ticker unavailable';
                copy.append(name, ticker);
                identity.append(copy);
                cell.append(identity);
                if (row.contract_address) {
                    const button = document.createElement('button');
                    button.className = 'table-action table-action--copy';
                    button.type = 'button';
                    button.dataset.action = 'copy';
                    button.dataset.copyValue = row.contract_address;
                    button.setAttribute('aria-label', `Copy ${row.ticker || row.name || 'token'} contract address`);
                    button.title = 'Copy contract address';
                    button.textContent = '⧉';
                    cell.append(button);
                }
            } else if (column.action === 'tokenIdentity') {
                const identity = document.createElement('div');
                identity.className = 'token-identity';
                const avatar = document.createElement('span');
                avatar.className = 'token-avatar';
                avatar.setAttribute('aria-hidden', 'true');
                avatar.textContent = String(row.ticker || value || '?').slice(0, 1).toUpperCase();
                if (row.image_url) {
                    const image = document.createElement('img');
                    image.className = 'token-avatar';
                    image.src = row.image_url;
                    image.alt = '';
                    image.loading = 'lazy';
                    image.addEventListener('error', () => image.replaceWith(avatar), { once: true });
                    identity.append(image);
                } else {
                    identity.append(avatar);
                }
                const copy = document.createElement('span');
                copy.className = 'token-identity-copy';
                const name = document.createElement('strong');
                name.textContent = value || row.contract_address || 'Unknown token';
                name.title = name.textContent;
                const ticker = document.createElement('small');
                ticker.textContent = row.ticker || row.contract_address || 'Name unavailable';
                ticker.title = ticker.textContent;
                copy.append(name, ticker);
                identity.append(copy);
                cell.append(identity);
            } else if (column.icon === 'chain') {
                const icon = document.createElement('span');
                const chain = String(value).toLowerCase();
                const symbols = { solana: '◎', ethereum: '◆', base: 'B', 'bnb chain': 'B', robinhood: 'R' };
                icon.className = `chain-icon chain-icon--${chain.replaceAll(' ', '-')}`;
                icon.textContent = symbols[chain] || chain.slice(0, 1).toUpperCase();
                icon.title = value;
                icon.setAttribute('aria-label', value);
                cell.append(icon);
            } else if (column.action === 'contract' && value !== '—') {
                const contract = document.createElement(row.fomo_url ? 'a' : 'span');
                contract.className = 'contract-value';
                contract.textContent = value;
                contract.title = value;
                if (row.fomo_url) {
                    contract.href = row.fomo_url;
                    contract.target = '_blank';
                    contract.rel = 'noopener noreferrer';
                    contract.title = 'Open this contract on Fomo Family';
                }
                cell.append(contract);
                if (row.contract_address) {
                    const button = document.createElement('button');
                    button.className = 'table-action table-action--copy';
                    button.type = 'button';
                    button.dataset.action = 'copy';
                    button.dataset.copyValue = row.contract_address;
                    button.setAttribute('aria-label', 'Copy token contract address');
                    button.title = 'Copy contract address';
                    button.textContent = '⧉';
                    cell.append(button);
                }
            } else if (column.action === 'buyers') {
                const button = document.createElement('button');
                button.className = 'table-action table-action--count';
                button.type = 'button';
                button.dataset.action = 'buyers';
                button.dataset.tokenId = String(row.token_id ?? row.id);
                button.dataset.tokenName = row.ticker || row.name || `Token #${row.token_id ?? row.id}`;
                button.textContent = value;
                button.setAttribute('aria-label', `Show ${value} traders who bought ${button.dataset.tokenName}`);
                cell.append(button);
            } else if (column.action === 'copy' && value !== '—') {
                const contract = document.createElement('span');
                contract.className = 'contract-value';
                contract.textContent = value;
                contract.title = value;
                const button = document.createElement('button');
                button.className = 'table-action table-action--copy';
                button.type = 'button';
                button.dataset.action = 'copy';
                button.dataset.copyValue = value;
                button.setAttribute('aria-label', 'Copy contract address');
                button.title = 'Copy contract address';
                button.textContent = '⧉';
                cell.append(contract, button);
            } else if (column.action === 'favorite') {
                if (row.is_favorite && activePage === 'favorites') {
                    const favorite = document.createElement('button');
                    favorite.className = 'table-action';
                    favorite.type = 'button';
                    favorite.dataset.action = 'unfavorite';
                    favorite.dataset.favoriteId = String(row.id);
                    favorite.textContent = '★';
                    favorite.title = 'Remove from favorites and return to tracked tokens';
                    favorite.setAttribute('aria-label', favorite.title);
                    cell.append(favorite);
                } else if (row.is_favorite) {
                    const favorite = document.createElement('span');
                    favorite.className = 'table-action';
                    favorite.textContent = '★';
                    favorite.title = 'Saved to favorites';
                    favorite.setAttribute('aria-label', 'Saved to favorites');
                    cell.append(favorite);
                } else {
                    const button = document.createElement('button');
                    button.className = 'table-action';
                    button.type = 'button';
                    button.dataset.action = 'favorite';
                    button.dataset.tokenId = String(row.token_id ?? row.id);
                    button.textContent = '☆';
                    button.title = 'Add to favorites';
                    button.setAttribute('aria-label', `Add ${row.ticker || row.name || 'token'} to favorites`);
                    cell.append(button);
                }
            } else if (column.action === 'delete') {
                const dismissRiskToken = activePage === 'risk';
                const deleteToken = ['overview', 'tokens', 'graduated'].includes(activePage);
                const deleteFavorite = activePage === 'favorites';
                const button = document.createElement('button');
                button.className = 'table-action table-action--delete';
                button.type = 'button';
                button.dataset.action = dismissRiskToken ? 'dismiss-risk-token'
                    : deleteFavorite ? 'delete-favorite' : deleteToken ? 'delete-token' : 'delete-user';
                button.dataset.rowId = String(['graduated', 'risk'].includes(activePage) ? row.token_id : row.id);
                button.dataset.rowName = row.username || row.ticker || row.name || `Token #${row.token_id ?? row.id}`;
                button.textContent = value;
                button.title = dismissRiskToken ? 'Move token to Dismissed'
                    : deleteFavorite ? 'Remove from favorites' : deleteToken ? 'Delete token' : 'Delete followed profile';
                button.setAttribute('aria-label', `${button.title}: ${button.dataset.rowName}`);
                cell.append(button);
            } else if (column.action === 'socials') {
                const links = Array.isArray(value) ? value : [];
                if (!links.length) cell.textContent = '—';
                links.forEach((item) => {
                    const link = document.createElement('a');
                    const label = String(item.label || 'Link');
                    const iconMap = { x: '𝕏', twitter: '𝕏', website: '◎', dexscreener: '◈', discord: 'D', telegram: '➤', instagram: '◎' };
                    link.className = 'social-link';
                    link.href = item.url;
                    link.target = '_blank';
                    link.rel = 'noopener noreferrer';
                    link.setAttribute('aria-label', `Open ${label}`);
                    link.title = `${label}: ${item.url}`;
                    const icon = document.createElement('span');
                    icon.className = `social-icon social-icon--${label.toLowerCase().replaceAll(/[^a-z0-9]+/g, '-')}`;
                    icon.setAttribute('aria-hidden', 'true');
                    icon.textContent = iconMap[label.toLowerCase()] || '↗';
                    link.append(icon);
                    cell.append(link);
                });
            } else if (column.action === 'profile' && value !== '—') {
                const link = document.createElement('a');
                link.className = 'table-link table-link--profile';
                link.href = row.profile_url || `https://fomo.family/profile/${encodeURIComponent(row.username || '')}`;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                link.textContent = value;
                cell.append(link);
            } else if (column.action === 'growth') {
                cell.textContent = value;
                const percentChange = column.growthPercent(row);
                if (percentChange !== null) {
                    cell.className = percentChange >= 0 ? 'growth-positive' : 'growth-negative';
                }
            } else if (column.badge) {
                const badge = document.createElement('span');
                badge.className = `badge badge--${column.toneFrom ? column.toneFrom(row) : column.tone || 'blue'}`;
                badge.textContent = value;
                cell.append(badge);
            } else {
                cell.textContent = value;
                if (column.primary) cell.className = 'cell-primary';
                if (column.mono) {
                    cell.className = 'cell-mono';
                    cell.title = column.title(row);
                }
                if (key === 'summary' || key === 'reason') cell.className = 'cell-muted';
            }
            tableRow.append(cell);
        });
        elements.tableBody.append(tableRow);
    });
    const totalKey = pageTotalKeys[activePage];
    const totalRecords = activeResultCount ?? (totalKey ? workspaceCounts[totalKey] : undefined);
    const pageIndex = pageIndexes.get(activePage) || 0;
    elements.count.textContent = activePage === 'overview'
        ? `Showing latest ${rows.length} bought tokens`
        : totalRecords !== undefined
        ? `Showing ${rows.length ? (pageIndex * pageSizeFor(activePage) + 1).toLocaleString() : 0}–${(pageIndex * pageSizeFor(activePage) + rows.length).toLocaleString()} of ${Number(totalRecords).toLocaleString()}`
        : query
        ? `${filteredRows.length} of ${rows.length} loaded records`
            : `${rows.length} ${rows.length === 1 ? 'record' : 'records'}`;
    elements.tableFrame.classList.toggle('is-empty', filteredRows.length === 0);
    elements.empty.hidden = filteredRows.length > 0;
    elements.emptyTitle.textContent = query ? 'No matching records' : pageDefinitions[activePage].emptyTitle;
    elements.emptyDescription.textContent = query ? 'Try a different search term to find a record.' : pageDefinitions[activePage].emptyDescription;
    updateTableControls();
}

function updateTableControls() {
    const selectedIds = selectedRowIds();
    const selectedCount = selectedIds.size;
    elements.deleteSelected.hidden = !bulkDeleteRoutes[activePage] || selectedCount === 0;
    elements.deleteSelected.disabled = deleteRowsInProgress;
    elements.deleteSelected.textContent = `Delete selected (${selectedCount})`;
    const pageCheckboxes = [...elements.tableBody.querySelectorAll('[data-action="select-row"]')];
    const selectPage = elements.tableHead.querySelector('[data-action="select-page"]');
    if (selectPage) {
        const selectedOnPage = pageCheckboxes.filter((checkbox) => checkbox.checked).length;
        selectPage.checked = pageCheckboxes.length > 0 && selectedOnPage === pageCheckboxes.length;
        selectPage.indeterminate = selectedOnPage > 0 && selectedOnPage < pageCheckboxes.length;
    }
    const pageIndex = pageIndexes.get(activePage) || 0;
    const totalRecords = activeResultCount ?? workspaceCounts[pageTotalKeys[activePage]];
    const currentPageSize = pageSizeFor(activePage);
    const pageCount = activePage === 'overview' ? 1 : totalRecords === undefined
        ? Math.max(1, pageIndex + (activeRows.length === currentPageSize ? 2 : 1))
        : Math.max(1, Math.ceil(Number(totalRecords) / currentPageSize));
    elements.pagination.replaceChildren();
    elements.pagination.hidden = activePage === 'ai' || activePage === 'overview' || pageCount <= 1;
    if (!elements.pagination.hidden) {
        const addButton = (label, targetPage, ariaLabel, current = false) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = `pagination-button${current ? ' is-current' : ''}`;
            button.dataset.pageIndex = String(targetPage);
            button.textContent = label;
            button.setAttribute('aria-label', ariaLabel);
            if (current) button.setAttribute('aria-current', 'page');
            elements.pagination.append(button);
        };
        const addEllipsis = () => {
            const ellipsis = document.createElement('span');
            ellipsis.className = 'pagination-ellipsis';
            ellipsis.textContent = '…';
            elements.pagination.append(ellipsis);
        };
        addButton('Previous', pageIndex - 1, 'Previous page');
        elements.pagination.lastElementChild.disabled = pageIndex === 0;

        const startPage = Math.max(0, Math.min(pageIndex - 2, pageCount - 5));
        const endPage = Math.min(pageCount, startPage + 5);
        if (startPage > 0) {
            addButton('1', 0, 'Go to page 1');
            if (startPage > 1) addEllipsis();
        }
        for (let index = startPage; index < endPage; index += 1) {
            addButton(String(index + 1), index, `Go to page ${index + 1}`, index === pageIndex);
        }
        if (endPage < pageCount) {
            if (endPage < pageCount - 1) addEllipsis();
            addButton(String(pageCount), pageCount - 1, `Go to page ${pageCount}`);
        }
        addButton('Next', pageIndex + 1, 'Next page');
        elements.pagination.lastElementChild.disabled = pageIndex >= pageCount - 1;
    }
    const pageLabel = activePage === 'overview'
        ? 'Showing the latest 50 bought tokens'
        : `Page ${pageIndex + 1} of ${pageCount} · ${currentPageSize} records per page`;
    elements.footnote.textContent = `${pageLabel}${selectedCount ? ` · ${selectedCount} selected` : ''}`;
}

function renderAiStatus(status) {
    const enabled = Boolean(status.enabled);
    const busy = ['scan_pending', 'scanning', 'model_pending'].includes(status.status);
    const progressLabel = status.status === 'model_pending' ? 'Qwen3 assessing…'
        : status.status === 'scanning' ? 'Scanning tracked tokens…'
            : 'Discovery queued…';
    const candidates = Array.isArray(status.candidates) ? status.candidates : [];
    elements.description.textContent = status.note || pageDefinitions.ai.description;
    elements.tableTitle.textContent = 'Tracked token candidates';
    setTableColumns(pageDefinitions.ai.columns);
    renderTable(candidates, pageDefinitions.ai.columns);
    elements.count.textContent = `${Number(status.candidate_count || 0)} candidates`;
    elements.aiRunButton.disabled = !enabled || busy;
    elements.aiRunButton.textContent = busy ? progressLabel : enabled ? 'Analyze tracked tokens' : 'AI discovery disabled';
    setMetrics([
        { label: 'Source', value: 'The Tokens', caption: 'Active tracked tokens only' },
        { label: 'Scanned', value: String(status.tokens_scanned || 0), caption: 'Active tokens evaluated' },
        { label: 'Results', value: String(status.candidate_count || 0), caption: 'Ranked candidates shown' },
        { label: 'Local model', value: status.status === 'model_pending' ? 'Running' : status.model_available ? 'Ready' : 'Fallback', caption: busy ? progressLabel : status.model || 'Ollama unavailable' },
    ]);
}

async function loadOverview() {
    const [overview, health, latestTokens] = await Promise.all([
        fetchJson('/dashboard/overview'),
        fetchJson('/health'),
        fetchJson(`${pageDefinitions.overview.endpoint}?limit=${overviewPageSize}&offset=0`),
    ]);
    elements.connection.textContent = health.status === 'ok' ? 'API connected' : 'API issue';
    elements.connectionDot.className = `connection-dot ${health.status === 'ok' ? 'is-online' : 'is-offline'}`;
    workspaceCounts = {
        ...overview,
        total_tokens: overview.total_tokens ?? overview.tokens,
    };
    updateNavigationCounts(overview);
    const metrics = [
        { label: 'Recent buys', value: String(latestTokens.length), caption: 'Latest bought contracts' },
        { label: 'Followed traders', value: String(overview.followed_traders ?? 0), tone: 'lime', caption: 'People on your watchlist' },
        { label: 'Graduations', value: String(overview.graduations ?? 0), tone: 'orange', caption: 'Threshold milestones' },
    ];
    return { rows: latestTokens, metrics };
}

function setLoading() {
    elements.error.hidden = true;
    elements.empty.hidden = true;
    elements.tableFrame.classList.remove('is-empty', 'is-error');
    elements.tableHead.replaceChildren();
    elements.tableBody.replaceChildren();
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 8;
    cell.textContent = 'Loading workspace data…';
    cell.className = 'cell-muted';
    row.append(cell);
    elements.tableBody.append(row);
    elements.count.textContent = 'Syncing';
}

async function loadPage(page = activePage, requestedPageIndex, searchQuery = page === activePage ? elements.search.value.trim() : '') {
    const definition = pageDefinitions[page];
    if (!definition) return;
    const totalRecords = workspaceCounts[pageTotalKeys[page]];
    let pageIndex = requestedPageIndex ?? pageIndexes.get(page) ?? 0;
    if (page === 'overview') pageIndex = 0;
    const currentPageSize = pageSizeFor(page);
    if (totalRecords !== undefined) {
        const pageCount = Math.max(1, Math.ceil(Number(totalRecords) / currentPageSize));
        pageIndex = Math.max(0, Math.min(pageIndex, pageCount - 1));
    }
    pageIndexes.set(page, pageIndex);
    const requestSequence = ++loadSequence;
    activePage = page;
    const pageCacheKey = `${page}:${searchQuery}:${pageIndex}:${page === 'followed' ? traderSort : ''}`;
    elements.error.hidden = true;
    elements.search.value = searchQuery;
    elements.searchControl.hidden = !searchablePages.has(page);
    elements.search.placeholder = page === 'followed' ? 'Search username or profile ID' : 'Search name, ticker, or contract';
    elements.traderSortControl.hidden = page !== 'followed';
    elements.aiRunButton.hidden = page !== 'ai';
    elements.traderSort.value = traderSort;
    activeResultCount = undefined;
    elements.title.textContent = definition.title;
    elements.breadcrumb.textContent = definition.breadcrumb;
    elements.description.textContent = definition.description;
    elements.eyebrow.textContent = definition.eyebrow;
    elements.kicker.textContent = definition.kicker;
    elements.tableTitle.textContent = definition.tableTitle;
    const cached = pageCache.get(pageCacheKey);
    elements.pagination.hidden = true;
    elements.deleteSelected.hidden = !bulkDeleteRoutes[page] || selectedRowIds(page).size === 0;
    const actionLabel = page === 'tokens' ? 'Add token' : page === 'followed' ? 'Add trader' : '';
    elements.primaryAction.hidden = !actionLabel;
    if (actionLabel) elements.actionLabel.textContent = actionLabel;
    document.querySelectorAll('.nav-item').forEach((item) => {
        const selected = item.dataset.page === page;
        item.classList.toggle('is-active', selected);
        if (selected) item.setAttribute('aria-current', 'page');
        else item.removeAttribute('aria-current');
    });
    document.getElementById('sidebar').classList.remove('is-open');
    document.getElementById('mobile-menu').setAttribute('aria-expanded', 'false');
    if (cached) {
        activeRows = cached.rows;
        activeResultCount = cached.totalCount;
        setMetrics(cached.metrics);
        setTableColumns(definition.columns);
        renderTable(activeRows, definition.columns);
    } else {
        setLoading();
    }
    elements.refresh.classList.add('is-loading');

    try {
        let pageMetrics;
        if (page === 'overview') {
            const overview = await loadOverview();
            if (requestSequence !== loadSequence || activePage !== page) return;
            activeRows = overview.rows;
            pageMetrics = overview.metrics;
            setMetrics(pageMetrics);
        } else {
            const pageParams = new URLSearchParams({ limit: String(currentPageSize), offset: String(pageIndex * currentPageSize) });
            if (page === 'followed') pageParams.set('sort_by', traderSort);
            if (searchablePages.has(page) && searchQuery) pageParams.set('search', searchQuery);
            const [result, health, searchCount] = await Promise.all([
                fetchJson(page === 'ai'
                    ? definition.endpoint
                    : `${definition.endpoint}?${pageParams}`),
                page === 'followed' ? fetchJson('/health').catch(() => null) : Promise.resolve(null),
                searchablePages.has(page) && searchQuery
                    ? fetchJson(`${searchCountEndpoints[page]}?search=${encodeURIComponent(searchQuery)}`)
                    : Promise.resolve(null),
            ]);
            if (requestSequence !== loadSequence || activePage !== page) return;
            if (page === 'followed') {
                followedMonitorStatus = health?.fomo_activity_monitor || { state: 'unknown', message: null };
                elements.description.textContent = followedMonitorStatus.message
                    ? `${definition.description} ${followedMonitorStatus.message}`
                    : definition.description;
            }
            if (page === 'ai') {
                renderAiStatus(result);
                elements.updated.textContent = `Checked ${new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
                return;
            }
            activeRows = Array.isArray(result) ? result : [];
            activeResultCount = searchCount?.count;
            pageMetrics = defaultMetrics(activeRows, page, activeResultCount);
            setMetrics(pageMetrics);
        }
        if (requestSequence !== loadSequence || activePage !== page) return;
        pageCache.set(pageCacheKey, { rows: activeRows, metrics: pageMetrics, totalCount: activeResultCount });
        setTableColumns(definition.columns);
        renderTable(activeRows, definition.columns);
        updateTableControls();
        elements.updated.textContent = `Updated ${new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
        elements.error.hidden = true;
        elements.tableFrame.classList.remove('is-error');
    } catch (error) {
        if (requestSequence !== loadSequence || activePage !== page) return;
        if (cached) {
            elements.updated.textContent = 'Refresh failed; showing saved data';
            return;
        }
        elements.tableFrame.classList.add('is-error');
        elements.error.hidden = false;
        elements.errorMessage.textContent = error instanceof Error ? error.message : 'Request failed';
        elements.count.textContent = 'Unavailable';
        elements.updated.textContent = 'Sync failed';
        if (page !== 'overview') setMetrics(defaultMetrics([], page));
    } finally {
        if (requestSequence === loadSequence) elements.refresh.classList.remove('is-loading');
    }
}

async function deleteSelectedRows() {
    const page = activePage;
    const config = bulkDeleteRoutes[page];
    const ids = [...selectedRowIds(page)];
    const confirmation = config?.action === 'dismiss'
        ? `Move ${ids.length} selected token${ids.length === 1 ? '' : 's'} to Dismissed? They will stop receiving updates.`
        : `Delete ${ids.length} selected ${config?.itemLabel}${ids.length === 1 ? '' : 's'}? This cannot be undone.`;
    if (!config || !ids.length || deleteRowsInProgress || !window.confirm(confirmation)) return;
    deleteRowsInProgress = true;
    elements.deleteSelected.disabled = true;
    try {
        const result = await fetchJson(config.endpoint, {
            method: config.action === 'dismiss' ? 'POST' : 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [config.bodyKey]: ids }),
        });
        selectedRowIds(page).clear();
        if (config.action === 'dismiss') {
            decrementWorkspaceCount('risk_findings', result.risk_findings_deleted);
            workspaceCounts.dismissed_tokens = Number(workspaceCounts.dismissed_tokens || 0) + result.dismissed;
            updateNavigationCounts(workspaceCounts);
            pageCache.clear();
            showToast(`${result.dismissed} token${result.dismissed === 1 ? '' : 's'} moved to Dismissed.`);
        } else {
            decrementWorkspaceCount(config.countKey, result.deleted);
            showToast(`${result.deleted} ${config.itemLabel}${result.deleted === 1 ? '' : 's'} deleted.`);
        }
        await loadPage(page);
    } catch (error) {
        showToast(error instanceof Error ? error.message : 'Could not delete selected rows.', true);
    } finally {
        deleteRowsInProgress = false;
        elements.deleteSelected.disabled = false;
    }
}

function showToast(message, isError = false) {
    clearTimeout(toastTimer);
    elements.toast.textContent = message;
    elements.toast.classList.toggle('is-error', isError);
    elements.toast.classList.add('is-visible');
    toastTimer = setTimeout(() => elements.toast.classList.remove('is-visible'), 3200);
}

function openEntryDialog() {
    const forTrader = activePage === 'followed';
    elements.tokenForm.hidden = forTrader;
    elements.traderForm.hidden = !forTrader;
    elements.dialog.showModal();
}

elements.nav.addEventListener('click', (event) => {
    const button = event.target.closest('[data-page]');
    if (button) loadPage(button.dataset.page);
});
elements.tableBody.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const tokenId = button.dataset.tokenId;
    if (button.dataset.action === 'copy') {
        try {
            await copyText(button.dataset.copyValue);
            showToast('Contract address copied.');
        } catch {
            showToast('Could not copy contract address.', true);
        }
        return;
    }
    if (button.dataset.action === 'favorite') {
        button.disabled = true;
        try {
            await fetchJson(`/favorites/${tokenId}`, { method: 'POST' });
            if (workspaceCounts.favorites !== undefined) workspaceCounts.favorites += 1;
            updateNavigationCounts(workspaceCounts);
            showToast('Token added to favorites.');
            await loadPage(activePage);
        } catch (error) {
            showToast(error instanceof Error ? error.message : 'Could not add favorite.', true);
            button.disabled = false;
        }
        return;
    }
    if (button.dataset.action === 'unfavorite') {
        button.disabled = true;
        try {
            await fetchJson(`/favorites/${button.dataset.favoriteId}`, { method: 'DELETE' });
            decrementWorkspaceCount('favorites');
            showToast('Favorite removed; token returned to Tracked tokens.');
            await loadPage(activePage);
        } catch (error) {
            showToast(error instanceof Error ? error.message : 'Could not remove favorite.', true);
            button.disabled = false;
        }
        return;
    }
    if (['delete-token', 'delete-user', 'delete-favorite', 'dismiss-risk-token'].includes(button.dataset.action)) {
        const deleteToken = button.dataset.action === 'delete-token';
        const deleteFavorite = button.dataset.action === 'delete-favorite';
        const dismissRiskToken = button.dataset.action === 'dismiss-risk-token';
        const itemType = dismissRiskToken ? 'token' : deleteFavorite ? 'favorite' : deleteToken ? 'token' : 'followed profile';
        const confirmation = dismissRiskToken
            ? `Move ${button.dataset.rowName} to Dismissed? It will stop receiving updates.`
            : `Delete ${itemType} ${button.dataset.rowName}? This cannot be undone.`;
        if (!window.confirm(confirmation)) return;
        button.disabled = true;
        try {
            const route = dismissRiskToken ? '/dismissed-tokens/bulk-dismiss'
                : deleteFavorite ? `/favorites/${button.dataset.rowId}`
                : deleteToken ? `/tokens/${button.dataset.rowId}`
                    : `/followed-traders/${button.dataset.rowId}`;
            const options = dismissRiskToken ? {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify({ token_ids: [Number(button.dataset.rowId)] }),
            } : { method: 'DELETE' };
            const result = await fetchJson(route, options);
            selectedRowIds().delete(Number(button.dataset.rowId));
            if (deleteToken) decrementWorkspaceCount(activePage === 'graduated' ? 'graduations' : 'tokens');
            else if (deleteFavorite) decrementWorkspaceCount('favorites');
            else if (dismissRiskToken) {
                decrementWorkspaceCount('risk_findings', result.risk_findings_deleted);
                workspaceCounts.dismissed_tokens = Number(workspaceCounts.dismissed_tokens || 0) + result.dismissed;
                updateNavigationCounts(workspaceCounts);
                pageCache.clear();
            }
            else decrementWorkspaceCount('followed_traders');
            showToast(dismissRiskToken ? 'Token moved to Dismissed.'
                : deleteFavorite ? 'Favorite removed.' : `${deleteToken ? 'Token' : 'Profile'} deleted.`);
            await loadPage(activePage);
        } catch (error) {
            showToast(error instanceof Error ? error.message : `Could not delete ${itemType}.`, true);
            button.disabled = false;
        }
        return;
    }
    if (button.dataset.action === 'buyers') {
        const dialog = document.getElementById('buyer-dialog');
        const list = document.getElementById('buyer-list');
        document.getElementById('buyer-title').textContent = `${button.dataset.tokenName} buyers`;
        list.replaceChildren();
        const loading = document.createElement('li');
        loading.textContent = 'Loading traders…';
        list.append(loading);
        dialog.showModal();
        try {
            const buyers = await fetchJson(`/tokens/${tokenId}/buyers`);
            list.replaceChildren();
            if (!buyers.length) {
                const empty = document.createElement('li');
                empty.textContent = 'No followed traders have recorded a buy for this token.';
                list.append(empty);
            }
            buyers.forEach((buyer) => {
                const item = document.createElement('li');
                const username = document.createElement('a');
                username.className = 'table-link table-link--profile';
                username.href = buyer.profile_url || `https://fomo.family/profile/${encodeURIComponent(buyer.username)}`;
                username.target = '_blank';
                username.rel = 'noopener noreferrer';
                username.textContent = buyer.username;
                const detail = document.createElement('span');
                detail.textContent = [money(buyer.estimated_usd), dateTime(buyer.event_time)].filter((part) => part !== '—').join(' · ');
                item.append(username, detail);
                list.append(item);
            });
        } catch (error) {
            list.replaceChildren();
            const item = document.createElement('li');
            item.textContent = error instanceof Error ? error.message : 'Could not load traders.';
            list.append(item);
        }
    }
});
elements.tableBody.addEventListener('change', (event) => {
    const checkbox = event.target.closest('[data-action="select-row"]');
    if (!checkbox) return;
    const rowId = Number(checkbox.dataset.rowId);
    if (checkbox.checked) selectedRowIds().add(rowId);
    else selectedRowIds().delete(rowId);
    updateTableControls();
});
elements.tableHead.addEventListener('change', (event) => {
    if (!event.target.matches('[data-action="select-page"]')) return;
    elements.tableBody.querySelectorAll('[data-action="select-row"]').forEach((checkbox) => {
        checkbox.checked = event.target.checked;
        const rowId = Number(checkbox.dataset.rowId);
        if (checkbox.checked) selectedRowIds().add(rowId);
        else selectedRowIds().delete(rowId);
    });
    updateTableControls();
});
elements.search.addEventListener('input', () => {
    if (searchablePages.has(activePage)) {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => loadPage(activePage, 0, elements.search.value.trim()), 250);
    } else if (activePage !== 'ai') {
        renderTable(activeRows, pageDefinitions[activePage].columns);
    }
});
elements.refresh.addEventListener('click', () => loadPage(activePage, undefined, elements.search.value.trim()));
elements.traderSort.addEventListener('change', () => {
    traderSort = elements.traderSort.value;
    loadPage('followed', 0, '');
});
elements.aiRunButton.addEventListener('click', async () => {
    elements.aiRunButton.disabled = true;
    elements.aiRunButton.textContent = 'Analyzing…';
    let busy = false;
    try {
        const result = await fetchJson('/ai-discovery/run', { method: 'POST' });
        if (activePage !== 'ai') return;
        busy = ['scan_pending', 'scanning', 'model_pending'].includes(result.status);
        renderAiStatus(result);
        elements.updated.textContent = `Analyzed ${new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
        showToast(busy ? 'Discovery started for active tracked tokens.' : `Analyzed ${result.tokens_scanned} active tracked tokens.`);
    } catch (error) {
        showToast(error instanceof Error ? error.message : 'AI analysis failed.', true);
    } finally {
        elements.aiRunButton.disabled = busy;
        elements.aiRunButton.textContent = busy ? (activePage === 'ai' ? elements.aiRunButton.textContent : 'Analyze tracked tokens') : 'Analyze tracked tokens';
    }
});
elements.pagination.addEventListener('click', (event) => {
    const button = event.target.closest('[data-page-index]');
    if (button && !button.disabled) loadPage(activePage, Number(button.dataset.pageIndex), elements.search.value.trim());
});
elements.deleteSelected.addEventListener('click', deleteSelectedRows);
document.getElementById('retry-button').addEventListener('click', () => loadPage(activePage));
elements.primaryAction.addEventListener('click', openEntryDialog);
document.querySelectorAll('[data-close-dialog]').forEach((button) => button.addEventListener('click', () => elements.dialog.close()));
document.querySelector('[data-close-buyers]').addEventListener('click', () => document.getElementById('buyer-dialog').close());
elements.dialog.addEventListener('click', (event) => {
    if (event.target === elements.dialog) elements.dialog.close();
});
document.getElementById('mobile-menu').addEventListener('click', (event) => {
    const sidebar = document.getElementById('sidebar');
    const isOpen = sidebar.classList.toggle('is-open');
    event.currentTarget.setAttribute('aria-expanded', String(isOpen));
});
document.addEventListener('keydown', (event) => {
    if (event.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
        event.preventDefault();
        elements.search.focus();
    }
    if (event.key === 'Escape') document.getElementById('sidebar').classList.remove('is-open');
});

elements.tokenForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const formData = new FormData(elements.tokenForm);
    const params = new URLSearchParams({ chain_name: formData.get('chain_name'), address: formData.get('address') });
    const submit = elements.tokenForm.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
        await fetchJson(`/tokens/?${params}`, { method: 'POST' });
        elements.dialog.close();
        elements.tokenForm.reset();
        showToast('Token added to tracking.');
        await loadPage(activePage);
    } catch (error) {
        showToast(error instanceof Error ? error.message : 'Could not add token.', true);
    } finally {
        submit.disabled = false;
    }
});

elements.traderForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const username = new FormData(elements.traderForm).get('username');
    const submit = elements.traderForm.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
        await fetchJson(`/followed-traders/?${new URLSearchParams({ username })}`, { method: 'POST' });
        elements.dialog.close();
        elements.traderForm.reset();
        showToast('Trader added to your watchlist.');
        await loadPage(activePage);
    } catch (error) {
        showToast(error instanceof Error ? error.message : 'Could not add trader.', true);
    } finally {
        submit.disabled = false;
    }
});

function updateClock() {
    document.getElementById('footer-clock').textContent = new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(new Date());
}

updateClock();
loadPage();
setInterval(() => {
    if (document.visibilityState === 'visible') loadPage(activePage);
}, 60_000);