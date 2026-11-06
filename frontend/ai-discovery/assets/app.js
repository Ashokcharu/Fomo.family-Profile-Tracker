const fields = {
    refresh: document.querySelector('#refresh'),
    search: document.querySelector('#search'),
    body: document.querySelector('#candidates'),
    status: document.querySelector('#status'),
    count: document.querySelector('#candidate-count'),
    scanned: document.querySelector('#tokens-scanned'),
    lastRun: document.querySelector('#last-run'),
    shown: document.querySelector('#shown-count'),
};
let candidates = [];
let loading = false;

function money(value) {
    const amount = Number(value);
    if (value == null || value === '' || !Number.isFinite(amount)) return '—';
    const abs = Math.abs(amount);
    const [scale, suffix] = abs >= 1e9 ? [1e9, 'b'] : abs >= 1e6 ? [1e6, 'm'] : abs >= 1e3 ? [1e3, 'k'] : [1, ''];
    return suffix
        ? `${amount < 0 ? '-$' : '$'}${(abs / scale).toLocaleString('en-US', { maximumFractionDigits: 2 })}${suffix}`
        : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(amount);
}

function growth(baseline, value) {
    if (baseline == null || value == null || !Number.isFinite(Number(baseline)) || Number(baseline) <= 0 || !Number.isFinite(Number(value))) return '—';
    const percent = (Number(value) / Number(baseline) - 1) * 100;
    return `${percent > 0 ? '+' : ''}${percent.toFixed(1)}%`;
}

function fomoTokenUrl(token) {
    if (typeof token.fomo_url === 'string') {
        try {
            const url = new URL(token.fomo_url);
            if (url.protocol === 'https:' && ['fomo.family', 'www.fomo.family'].includes(url.hostname.toLowerCase())) {
                return url.href;
            }
        } catch {
            // Fall back to the canonical token URL below.
        }
    }
    if (!token.contract_address || !token.chain_name) return null;
    const chain = String(token.chain_name).toLowerCase().replaceAll('_', '-');
    const slugs = { 'bnb-chain': 'bnb', bsc: 'bnb', 'robinhood-chain': 'robinhood' };
    return `https://fomo.family/tokens/${encodeURIComponent(slugs[chain] || chain)}/${encodeURIComponent(token.contract_address)}`;
}

function cell(row, value, className = '') {
    const td = document.createElement('td');
    td.textContent = value == null || value === '' ? '—' : String(value);
    if (className) td.className = className;
    row.append(td);
}

function render() {
    const query = fields.search.value.trim().toLocaleLowerCase();
    const rows = candidates.filter((token) => [token.name, token.ticker, token.contract_address]
        .some((value) => String(value || '').toLocaleLowerCase().includes(query)));
    fields.body.replaceChildren();
    fields.shown.textContent = `${rows.length.toLocaleString()} candidate${rows.length === 1 ? '' : 's'}`;
    if (!rows.length) {
        const row = document.createElement('tr');
        const td = document.createElement('td');
        td.className = 'empty';
        td.colSpan = 15;
        td.textContent = candidates.length ? 'No candidates match this search.' : 'No AI Discovery candidates are available.';
        row.append(td);
        fields.body.append(row);
        return;
    }

    for (const token of rows) {
        const row = document.createElement('tr');
        cell(row, `#${token.rank ?? '—'}`, 'rank');
        const identity = document.createElement('td');
        const name = document.createElement('strong');
        name.textContent = token.name || token.ticker || 'Unknown token';
        const tokenUrl = fomoTokenUrl(token);
        if (tokenUrl) {
            const nameLink = document.createElement('a');
            nameLink.href = tokenUrl;
            nameLink.target = '_blank';
            nameLink.rel = 'noopener noreferrer';
            nameLink.append(name);
            identity.append(nameLink);
        } else {
            identity.append(name);
        }
        const ticker = document.createElement('small');
        ticker.textContent = token.ticker || token.contract_address || 'Ticker unavailable';
        identity.append(ticker);
        if (token.contract_address) {
            const contract = document.createElement('div');
            contract.className = 'token-contract';
            const address = document.createElement('span');
            address.textContent = token.contract_address;
            address.title = token.contract_address;
            const copyButton = document.createElement('button');
            copyButton.className = 'copy-address';
            copyButton.type = 'button';
            copyButton.dataset.copyAddress = token.contract_address;
            copyButton.setAttribute('aria-label', `Copy contract address for ${token.name || token.ticker || 'token'}`);
            copyButton.textContent = 'Copy CA';
            contract.append(address, copyButton);
            identity.append(contract);
        }
        row.append(identity);
        const since = Date.parse(token.ai_discovery_since);
        cell(row, Number.isFinite(since) ? `${String(Math.max(0, Math.floor((Date.now() - since) / 3_600_000))).padStart(2, '0')}h` : '—');
        cell(row, token.is_favorite ? '★' : '☆');
        cell(row, token.score == null ? '—' : `${Number(token.score).toFixed(1)} / 100`);
        cell(row, token.risk_score == null ? 'Unknown' : `${Number(token.risk_score).toFixed(0)} / 100`);
        cell(row, token.data_completeness_percent == null ? 'Unknown' : `${Number(token.data_completeness_percent).toFixed(0)}%`);
        cell(row, money(token.market_cap));
        cell(row, money(token.ai_rmc));
        cell(row, money(token.ai_rh));
        cell(row, growth(token.ai_rmc, token.market_cap));
        cell(row, growth(token.ai_rmc, token.ai_rh));
        cell(row, money(token.liquidity));
        cell(row, token.buyer_count ?? 0);
        cell(row, token.fomo_holder_count);
        fields.body.append(row);
    }
}

async function copyContractAddress(event) {
    const button = event.target.closest('button[data-copy-address]');
    if (!button) return;
    try {
        await navigator.clipboard.writeText(button.dataset.copyAddress);
        fields.status.textContent = 'Contract address copied to clipboard.';
        button.textContent = 'Copied';
    } catch {
        fields.status.textContent = 'Could not copy the contract address. Select and copy the address shown in the token row.';
    }
}

async function loadCandidates() {
    if (loading) return;
    loading = true;
    fields.refresh.disabled = true;
    try {
        const response = await fetch('/api/ai-discovery', { headers: { Accept: 'application/json' }, cache: 'no-store' });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status}).`);
        if (!Array.isArray(payload.candidates)) throw new Error('Invalid AI Discovery response.');
        candidates = payload.candidates;
        fields.count.textContent = Number(payload.candidate_count).toLocaleString();
        fields.scanned.textContent = Number(payload.tokens_scanned).toLocaleString();
        fields.lastRun.textContent = payload.last_run_at
            ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(payload.last_run_at))
            : '—';
        fields.status.textContent = `Status: ${payload.status}`;
        render();
    } catch (error) {
        fields.status.textContent = error instanceof Error ? error.message : 'Unable to load AI Discovery candidates.';
        if (!candidates.length) {
            fields.body.replaceChildren();
            const row = document.createElement('tr');
            const td = document.createElement('td');
            td.className = 'empty';
            td.colSpan = 15;
            td.textContent = 'Candidate data could not be loaded.';
            row.append(td);
            fields.body.append(row);
        }
    } finally {
        loading = false;
        fields.refresh.disabled = false;
    }
}

fields.refresh.addEventListener('click', loadCandidates);
fields.search.addEventListener('input', render);
fields.body.addEventListener('click', copyContractAddress);
loadCandidates();
window.setInterval(loadCandidates, 60_000);
