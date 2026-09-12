'use client';

import { useEffect, useMemo, useState } from 'react';

type FilterState = {
  category: string;
  loyalty: string;
  activeYears: string;
  frequency: string;
  search: string;
};

type Distribution = { label: string; count: number; share: number };
type CategoryDistribution = Distribution & { revenue: number; loyal_rate: number };
type MemberRecord = {
  member_id: string;
  is_loyal: boolean;
  total_tx: number;
  total_amount: number;
  avg_amount: number;
  active_years: number;
  tx_per_year: number;
  avg_gap_days: number;
  top_category: string;
  top_cat_ratio: number;
};

type DashboardData = {
  meta: {
    source: string;
    mode: string;
    generated_at: string;
    version: string;
    schema_version?: string;
    source_rows: number;
    filtered_rows: number;
    api_contract: string;
  };
  filter_options: {
    categories: string[];
    active_years: number[];
    frequency_bands: string[];
  };
  summary: {
    member_count: number;
    transaction_count: number;
    total_revenue: number;
    avg_transaction_value: number;
    loyal_count: number;
    loyal_rate: number;
    repeat_member_rate: number;
    avg_gap_days: number | null;
    avg_gap_sample: number;
  };
  distributions: {
    categories: CategoryDistribution[];
    active_years: Distribution[];
    frequency: Distribution[];
    loyalty: Distribution[];
  };
  insights: {
    top_category: CategoryDistribution | null;
    loyal_revenue_share: number;
    repeat_member_rate: number;
    valid_gap_sample: number;
  };
  records: MemberRecord[];
  pagination: { page: number; page_size: number; page_count: number; total: number };
};

const initialFilters: FilterState = {
  category: 'all',
  loyalty: 'all',
  activeYears: 'all',
  frequency: 'all',
  search: '',
};

const number = new Intl.NumberFormat('zh-TW');
const money = new Intl.NumberFormat('zh-TW', {
  style: 'currency',
  currency: 'TWD',
  maximumFractionDigits: 0,
});

const configuredApiUrl = process.env.NEXT_PUBLIC_DASHBOARD_API_URL?.trim() ?? '';
const isRemoteApi = configuredApiUrl.length > 0;
const isGcpIapMode = process.env.NEXT_PUBLIC_DEPLOYMENT_MODE === 'gcp-iap';
const apiKeySessionStorageKey = 'yijin-dashboard-api-key';
const apiBase = (() => {
  if (!configuredApiUrl) return '/api/dashboard';
  const normalized = configuredApiUrl.replace(/\/+$/, '');
  return normalized.endsWith('/v1/dashboard') ? normalized : `${normalized}/v1/dashboard`;
})();

async function apiErrorMessage(response: Response) {
  try {
    const payload = await response.json() as { error?: { message?: string } };
    if (payload.error?.message) return payload.error.message;
  } catch {
    // Fall through to the localized status message.
  }

  if (response.status === 400) return '篩選條件格式不正確，請調整後再試。';
  if (response.status === 401) return 'API Key 無效或尚未提供，請重新輸入。';
  if (response.status === 503) return '分析資料尚未準備完成，請稍後再試。';
  return `儀表板服務暫時無法使用（HTTP ${response.status}）。`;
}

function formatUpdatedAt(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '時間資訊無法辨識' : parsed.toLocaleString('zh-TW');
}

function SelectField({
  label,
  value,
  onChange,
  children,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  children: React.ReactNode;
}) {
  return (
    <label className="grid gap-2">
      <span className="text-xs font-semibold text-[#566761]">{label}</span>
      <select
        className="w-full rounded-xl border border-[#19322d]/15 bg-[#fbfaf6] px-3 py-2.5 text-sm outline-none transition focus:border-[#b46346] focus:ring-2 focus:ring-[#b46346]/10"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {children}
      </select>
    </label>
  );
}

function EmptyState() {
  return (
    <div className="grid min-h-56 place-items-center rounded-2xl border border-dashed border-[#19322d]/20 bg-[#fbfaf6] px-6 text-center">
      <div>
        <div className="mx-auto mb-3 h-9 w-9 rounded-full border-8 border-[#e9e5dc] border-t-[#b46346]" />
        <p className="font-medium">目前篩選條件沒有資料</p>
        <p className="mt-1 text-xs text-[#7f8c88]">請調整篩選條件或重設篩選。</p>
      </div>
    </div>
  );
}

function ApiKeyGate({
  value,
  error,
  onChange,
  onSubmit,
}: {
  value: string;
  error: string;
  onChange: (value: string) => void;
  onSubmit: (event: React.FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-[#102e29]/80 px-4 backdrop-blur-md">
      <section className="w-full max-w-md rounded-3xl border border-white/20 bg-[#f8f4eb] p-6 shadow-2xl sm:p-8" role="dialog" aria-modal="true" aria-labelledby="api-key-title">
        <div className="mb-6 flex items-center gap-3">
          <div className="grid h-11 w-11 shrink-0 place-items-center rounded-full bg-[#173c35] text-xs font-bold tracking-[0.1em] text-white">億進</div>
          <div>
            <p className="text-[10px] font-semibold tracking-[0.16em] text-[#b46346]">SECURE ACCESS</p>
            <h2 id="api-key-title" className="mt-1 text-xl font-semibold text-[#19322d]">輸入 Dashboard API Key</h2>
          </div>
        </div>

        <p className="text-sm leading-6 text-[#5a6c66]">請輸入管理者提供的金鑰。驗證成功後，才能讀取會員分析資料與下載 CSV。</p>

        <form className="mt-6 grid gap-4" onSubmit={onSubmit}>
          <label className="grid gap-2">
            <span className="text-xs font-semibold text-[#566761]">API Key</span>
            <input
              type="password"
              value={value}
              onChange={(event) => onChange(event.target.value)}
              autoComplete="current-password"
              autoFocus
              spellCheck={false}
              className="w-full rounded-xl border border-[#19322d]/20 bg-white px-4 py-3 font-mono text-sm outline-none transition focus:border-[#b46346] focus:ring-2 focus:ring-[#b46346]/10"
              aria-invalid={Boolean(error)}
              aria-describedby={error ? 'api-key-error' : 'api-key-note'}
            />
          </label>

          {error ? (
            <p id="api-key-error" className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</p>
          ) : (
            <p id="api-key-note" className="text-xs leading-5 text-[#75837e]">金鑰只保留在目前的瀏覽器分頁，關閉分頁後需重新輸入。</p>
          )}

          <button type="submit" className="rounded-xl bg-[#173c35] px-4 py-3 text-sm font-semibold text-white transition hover:bg-[#255b50]">驗證並開啟儀表板</button>
        </form>
      </section>
    </div>
  );
}

export default function Home() {
  const [filters, setFilters] = useState<FilterState>(initialFilters);
  const [page, setPage] = useState(1);
  const [refreshKey, setRefreshKey] = useState(0);
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [draftApiKey, setDraftApiKey] = useState('');
  const [authReady, setAuthReady] = useState(!isRemoteApi);
  const [requiresApiKey, setRequiresApiKey] = useState(isRemoteApi);
  const [authError, setAuthError] = useState('');

  useEffect(() => {
    if (!isRemoteApi) return;
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      const savedApiKey = window.sessionStorage.getItem(apiKeySessionStorageKey) ?? '';
      setApiKey(savedApiKey);
      setRequiresApiKey(savedApiKey.length === 0);
      setAuthReady(true);
    });
    return () => { active = false; };
  }, []);

  const query = useMemo(() => {
    const params = new URLSearchParams({
      category: filters.category,
      loyalty: filters.loyalty,
      activeYears: filters.activeYears,
      frequency: filters.frequency,
      search: filters.search,
      page: String(page),
      pageSize: '12',
    });
    return params.toString();
  }, [filters, page]);

  useEffect(() => {
    if (!authReady || requiresApiKey || (isRemoteApi && !apiKey)) {
      return;
    }

    const controller = new AbortController();

    async function loadDashboard() {
      await Promise.resolve();
      if (controller.signal.aborted) return;
      setLoading(true);
      setError('');
      try {
        const response = await fetch(`${apiBase}?${query}`, {
          signal: controller.signal,
          headers: isRemoteApi ? { Authorization: `Bearer ${apiKey}` } : undefined,
        });
        if (response.status === 401) {
          window.sessionStorage.removeItem(apiKeySessionStorageKey);
          setApiKey('');
          setRequiresApiKey(true);
          setAuthError('API Key 無效，請確認後重新輸入。');
          setError('');
          return;
        }
        if (!response.ok) throw new Error(await apiErrorMessage(response));
        const result = await response.json() as DashboardData;
        setData(result);
      } catch (reason) {
        if (reason instanceof Error && reason.name !== 'AbortError') {
          setError(reason.message || '無法連線至儀表板服務，請確認網路後再試。');
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }

    void loadDashboard();
    return () => controller.abort();
  }, [apiKey, authReady, query, refreshKey, requiresApiKey]);

  function submitApiKey(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedApiKey = draftApiKey.trim();
    if (!normalizedApiKey) {
      setAuthError('請輸入 API Key。');
      return;
    }
    window.sessionStorage.setItem(apiKeySessionStorageKey, normalizedApiKey);
    setApiKey(normalizedApiKey);
    setDraftApiKey('');
    setAuthError('');
    setRequiresApiKey(false);
  }

  function forgetApiKey() {
    window.sessionStorage.removeItem(apiKeySessionStorageKey);
    setApiKey('');
    setDraftApiKey('');
    setAuthError('');
    setRequiresApiKey(true);
    setData(null);
    setError('');
  }

  function updateFilter(key: keyof FilterState, value: string) {
    setFilters((current) => ({ ...current, [key]: value }));
    setPage(1);
  }

  async function downloadCsv() {
    setDownloading(true);
    setError('');
    try {
      const response = await fetch(`${apiBase}?${query}&download=csv`, {
        headers: isRemoteApi ? { Authorization: `Bearer ${apiKey}` } : undefined,
      });
      if (response.status === 401) {
        forgetApiKey();
        setAuthError('API Key 已失效，請重新輸入。');
        return;
      }
      if (!response.ok) throw new Error(await apiErrorMessage(response));
      const downloadUrl = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = downloadUrl;
      link.download = 'yijin-members-filtered.csv';
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(downloadUrl);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'CSV 下載失敗，請稍後再試。');
    } finally {
      setDownloading(false);
    }
  }

  const categoryMax = Math.max(...(data?.distributions.categories.map((item) => item.count) ?? [1]));
  const revenueMax = Math.max(...(data?.distributions.categories.map((item) => item.revenue) ?? [1]));
  const activeYearsMax = Math.max(...(data?.distributions.active_years.map((item) => item.count) ?? [1]));
  const loyalShare = data?.distributions.loyalty[0]?.share ?? 0;
  const activeFilterCount = Object.entries(filters).filter(([key, value]) => key === 'search' ? value !== '' : value !== 'all').length;

  return (
    <main className="min-h-screen bg-[#f3efe6] text-[#19322d]">
      {isRemoteApi && authReady && requiresApiKey && (
        <ApiKeyGate
          value={draftApiKey}
          error={authError}
          onChange={(value) => { setDraftApiKey(value); setAuthError(''); }}
          onSubmit={submitApiKey}
        />
      )}
      <header className="sticky top-0 z-30 border-b border-white/10 bg-[#173c35]/95 text-white backdrop-blur">
        <div className="mx-auto flex max-w-[1600px] items-center justify-between gap-4 px-4 py-4 sm:px-7">
          <div className="flex items-center gap-3 sm:gap-4">
            <div className="grid h-10 w-10 shrink-0 place-items-center rounded-full border border-white/25 bg-white/10 text-xs font-bold tracking-[0.1em] sm:h-11 sm:w-11">億進</div>
            <div>
              <p className="text-[10px] tracking-[0.2em] text-[#d9c8a6] sm:text-xs">億進寢具</p>
              <h1 className="text-base font-semibold tracking-tight sm:text-xl">會員行為洞察中心</h1>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <div className="hidden items-center gap-2 rounded-full bg-white/10 px-3 py-2 text-xs text-white/80 md:flex">
              <span className={`h-2 w-2 rounded-full ${error ? 'bg-red-300' : 'bg-[#a9d8bc]'}`} />
              {error ? 'API 連線異常' : isGcpIapMode ? 'GCP IAP 模式' : isRemoteApi ? 'GCP API 模式' : '本機模擬模式'}
            </div>
            {isRemoteApi && authReady && !requiresApiKey && (
              <button
                type="button"
                onClick={forgetApiKey}
                className="hidden rounded-xl border border-white/15 bg-white/10 px-3 py-2 text-xs font-medium transition hover:bg-white/15 sm:block"
              >
                更換 API Key
              </button>
            )}
            <button
              type="button"
              onClick={() => setRefreshKey((value) => value + 1)}
              className="rounded-xl border border-white/15 bg-white/10 px-3 py-2 text-xs font-medium transition hover:bg-white/15"
              aria-label="重新整理 API 資料"
            >
              重新整理
            </button>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-[1600px] px-4 py-6 sm:px-7 sm:py-8">
        <section className="mb-7 flex flex-col justify-between gap-4 lg:flex-row lg:items-end">
          <div>
            <div className="mb-2 flex items-center gap-3">
              <span className="h-px w-8 bg-[#b46346]" />
              <p className="text-xs font-semibold tracking-[0.16em] text-[#b46346]">CUSTOMER INTELLIGENCE</p>
            </div>
            <h2 className="max-w-3xl text-3xl font-semibold tracking-[-0.04em] sm:text-4xl lg:text-5xl">看懂會員，也看見下一次回購。</h2>
          </div>
          <p className="max-w-xl text-sm leading-6 text-[#5a6c66]">
            將 GCP 清理後的會員特徵，透過單一 API 轉換成管理視角。{isGcpIapMode ? '目前由 Google 登入與 IAP 保護存取。' : '未設定正式 API 時，畫面會使用去識別化的本機測試資料。'}
          </p>
        </section>

        <div className="grid items-start gap-5 xl:grid-cols-[270px_minmax(0,1fr)]">
          <aside className="rounded-3xl border border-[#19322d]/10 bg-white p-5 xl:sticky xl:top-[92px]">
            <div className="mb-5 flex items-center justify-between">
              <div>
                <p className="text-xs font-semibold tracking-[0.12em] text-[#b46346]">FILTERS</p>
                <h3 className="mt-1 text-lg font-semibold">篩選會員</h3>
              </div>
              {activeFilterCount > 0 && <span className="rounded-full bg-[#b46346] px-2 py-1 text-[10px] font-bold text-white">{activeFilterCount}</span>}
            </div>

            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-1">
              <label className="grid gap-2 sm:col-span-2 xl:col-span-1">
                <span className="text-xs font-semibold text-[#566761]">搜尋會員編號</span>
                <input
                  value={filters.search}
                  onChange={(event) => updateFilter('search', event.target.value)}
                  placeholder="例如 M-0021"
                  className="w-full rounded-xl border border-[#19322d]/15 bg-[#fbfaf6] px-3 py-2.5 text-sm outline-none transition placeholder:text-[#9ca6a2] focus:border-[#b46346] focus:ring-2 focus:ring-[#b46346]/10"
                />
              </label>

              <SelectField label="主要消費品類" value={filters.category} onChange={(value) => updateFilter('category', value)}>
                <option value="all">全部品類</option>
                {data?.filter_options.categories.map((option) => <option key={option} value={option}>{option}</option>)}
              </SelectField>

              <SelectField label="會員類型" value={filters.loyalty} onChange={(value) => updateFilter('loyalty', value)}>
                <option value="all">全部會員</option>
                <option value="loyal">忠誠會員</option>
                <option value="non-loyal">一般會員</option>
              </SelectField>

              <SelectField label="活躍年數" value={filters.activeYears} onChange={(value) => updateFilter('activeYears', value)}>
                <option value="all">全部年數</option>
                {data?.filter_options.active_years.map((option) => <option key={option} value={option}>{option} 年</option>)}
              </SelectField>

              <SelectField label="累積交易頻次" value={filters.frequency} onChange={(value) => updateFilter('frequency', value)}>
                <option value="all">全部頻次</option>
                {data?.filter_options.frequency_bands.map((option) => <option key={option} value={option}>{option}</option>)}
              </SelectField>
            </div>

            <button
              type="button"
              onClick={() => { setFilters(initialFilters); setPage(1); }}
              className="mt-5 w-full rounded-xl border border-[#19322d]/15 px-4 py-2.5 text-sm font-semibold transition hover:bg-[#f3efe6]"
            >
              重設全部篩選
            </button>

            <div className="mt-5 border-t border-[#19322d]/10 pt-5 text-xs leading-5 text-[#75837e]">
              <p className="font-semibold text-[#3f544e]">資料介接狀態</p>
              <p className="mt-1 break-all">{data?.meta.source ?? '等待 API'}</p>
              <p>API contract: {data?.meta.api_contract ?? '—'}</p>
              <p>資料版本：{data?.meta.version ?? '—'}</p>
            </div>
          </aside>

          <div className="min-w-0">
            {error && (
              <div className="mb-5 flex items-center justify-between gap-4 rounded-2xl border border-red-200 bg-red-50 px-5 py-4 text-sm text-red-700">
                <span>{error}</span>
                <button type="button" onClick={() => setRefreshKey((value) => value + 1)} className="shrink-0 font-semibold underline">重試</button>
              </div>
            )}

            <section className={`grid gap-3 sm:grid-cols-2 2xl:grid-cols-5 ${loading ? 'opacity-60' : ''}`} aria-busy={loading}>
              {[
                { label: '會員數', value: number.format(data?.summary.member_count ?? 0), note: `占目前來源 ${data?.meta.source_rows ? ((data.summary.member_count / data.meta.source_rows) * 100).toFixed(1) : '0.0'}%`, tone: 'paper' },
                { label: '累積交易次數', value: number.format(data?.summary.transaction_count ?? 0), note: '篩選會員交易合計', tone: 'paper' },
                { label: '累積消費金額', value: money.format(data?.summary.total_revenue ?? 0), note: '篩選會員消費合計', tone: 'paper' },
                { label: '平均交易金額', value: money.format(data?.summary.avg_transaction_value ?? 0), note: '消費金額 ÷ 交易次數', tone: 'paper' },
                { label: '忠誠會員占比', value: `${(data?.summary.loyal_rate ?? 0).toFixed(1)}%`, note: `有效會員 ${number.format(data?.summary.member_count ?? 0)} 位`, tone: 'accent' },
              ].map((item) => (
                <article key={item.label} className={`min-h-[142px] rounded-2xl border p-5 ${item.tone === 'accent' ? 'border-[#b46346] bg-[#b46346] text-white' : 'border-[#19322d]/10 bg-white'}`}>
                  <p className={`text-xs font-semibold ${item.tone === 'accent' ? 'text-white/70' : 'text-[#66756f]'}`}>{item.label}</p>
                  <p className="mt-4 text-[clamp(1.45rem,2.2vw,2rem)] font-semibold tracking-[-0.045em]">{item.value}</p>
                  <p className={`mt-2 text-[11px] ${item.tone === 'accent' ? 'text-white/70' : 'text-[#8b9692]'}`}>{item.note}</p>
                </article>
              ))}
            </section>

            {!data || data.summary.member_count === 0 ? (
              <div className="mt-5"><EmptyState /></div>
            ) : (
              <>
                <section className="mt-5 grid gap-5 lg:grid-cols-[minmax(0,1.7fr)_minmax(280px,0.7fr)]">
                  <article className="rounded-3xl border border-[#19322d]/10 bg-white p-5 sm:p-7">
                    <div className="mb-6 flex items-end justify-between gap-4">
                      <div>
                        <p className="text-[10px] font-semibold tracking-[0.15em] text-[#b46346]">CATEGORY MIX</p>
                        <h3 className="mt-1 text-xl font-semibold">主要消費品類分布</h3>
                      </div>
                      <p className="text-xs text-[#87938f]">會員數 / 占比</p>
                    </div>
                    <div className="grid gap-3">
                      {data.distributions.categories.slice(0, 7).map((item) => (
                        <div key={item.label} className="grid grid-cols-[82px_minmax(0,1fr)_48px] items-center gap-3 text-sm sm:grid-cols-[120px_minmax(0,1fr)_70px]">
                          <span className="truncate font-medium" title={item.label}>{item.label}</span>
                          <div className="h-8 overflow-hidden rounded-md bg-[#edf0ec]">
                            <div className="flex h-full items-center rounded-md bg-[#255b50] px-2.5 text-[10px] text-white transition-all duration-300 sm:text-xs" style={{ width: `${Math.max(8, (item.count / categoryMax) * 100)}%` }}>
                              {item.share.toFixed(1)}%
                            </div>
                          </div>
                          <span className="text-right font-mono text-xs text-[#64736f]">{number.format(item.count)}</span>
                        </div>
                      ))}
                    </div>
                  </article>

                  <article className="rounded-3xl bg-[#173c35] p-6 text-white sm:p-7">
                    <p className="text-[10px] font-semibold tracking-[0.15em] text-[#d7c7a8]">QUICK READ</p>
                    <h3 className="mt-1 text-xl font-semibold">本次篩選洞察</h3>
                    <div className="mt-6 grid gap-5">
                      <div className="border-b border-white/10 pb-5">
                        <p className="text-xs text-white/55">最大會員品類</p>
                        <p className="mt-1 text-2xl font-semibold">{data.insights.top_category?.label ?? '—'}</p>
                        <p className="mt-1 text-xs text-white/60">{data.insights.top_category?.share.toFixed(1) ?? 0}% 的會員以此為主要品類</p>
                      </div>
                      <div className="border-b border-white/10 pb-5">
                        <p className="text-xs text-white/55">忠誠會員消費貢獻</p>
                        <p className="mt-1 text-2xl font-semibold">{data.insights.loyal_revenue_share.toFixed(1)}%</p>
                        <p className="mt-1 text-xs text-white/60">忠誠會員消費額 ÷ 篩選會員消費額</p>
                      </div>
                      <div>
                        <p className="text-xs text-white/55">重複交易會員率</p>
                        <p className="mt-1 text-2xl font-semibold">{data.insights.repeat_member_rate.toFixed(1)}%</p>
                        <p className="mt-1 text-xs text-white/60">累積交易超過 1 筆的會員</p>
                      </div>
                    </div>
                  </article>
                </section>

                <section className="mt-5 grid gap-5 lg:grid-cols-2">
                  <article className="rounded-3xl border border-[#19322d]/10 bg-white p-5 sm:p-7">
                    <div className="mb-7 flex items-end justify-between gap-4">
                      <div>
                        <p className="text-[10px] font-semibold tracking-[0.15em] text-[#b46346]">RETENTION DEPTH</p>
                        <h3 className="mt-1 text-xl font-semibold">會員活躍年數</h3>
                      </div>
                      <p className="text-xs text-[#87938f]">資料期間最多 4 年</p>
                    </div>
                    <div className="flex h-56 items-end justify-around gap-4 border-b border-[#19322d]/10 px-2">
                      {data.distributions.active_years.map((item, index) => (
                        <div key={item.label} className="flex h-full flex-1 flex-col items-center justify-end">
                          <span className="mb-2 text-xs font-semibold">{number.format(item.count)}</span>
                          <div
                            className={`w-full max-w-16 rounded-t-xl ${index === 0 ? 'bg-[#b46346]' : 'bg-[#255b50]'}`}
                            style={{ height: `${Math.max(10, (item.count / activeYearsMax) * 78)}%` }}
                          />
                          <span className="-mb-7 mt-2 text-xs text-[#66756f]">{item.label}</span>
                        </div>
                      ))}
                    </div>
                    <p className="mt-10 text-xs text-[#7f8c88]">可搭配「活躍年數」篩選，比較不同留存深度的消費結構。</p>
                  </article>

                  <article className="rounded-3xl border border-[#19322d]/10 bg-white p-5 sm:p-7">
                    <div className="mb-6">
                      <p className="text-[10px] font-semibold tracking-[0.15em] text-[#b46346]">PURCHASE FREQUENCY</p>
                      <h3 className="mt-1 text-xl font-semibold">累積交易頻次</h3>
                    </div>
                    <div className="grid gap-4">
                      {data.distributions.frequency.map((item, index) => (
                        <div key={item.label}>
                          <div className="mb-1.5 flex items-center justify-between text-xs">
                            <span className="font-medium">{item.label}</span>
                            <span className="font-mono text-[#66756f]">{number.format(item.count)} · {item.share.toFixed(1)}%</span>
                          </div>
                          <div className="h-3 overflow-hidden rounded-full bg-[#edf0ec]">
                            <div className={`h-full rounded-full ${index === 3 ? 'bg-[#b46346]' : 'bg-[#255b50]'}`} style={{ width: `${item.share}%` }} />
                          </div>
                        </div>
                      ))}
                    </div>
                    <div className="mt-6 grid grid-cols-[100px_1fr] items-center gap-5 border-t border-[#19322d]/10 pt-5">
                      <div className="grid h-24 w-24 place-items-center rounded-full" style={{ background: `conic-gradient(#b46346 0 ${loyalShare}%, #e5e0d6 ${loyalShare}% 100%)` }}>
                        <div className="grid h-16 w-16 place-items-center rounded-full bg-white text-center">
                          <span className="text-sm font-semibold">{loyalShare.toFixed(1)}%</span>
                        </div>
                      </div>
                      <div>
                        <p className="font-semibold">忠誠會員結構</p>
                        <p className="mt-1 text-xs leading-5 text-[#71807b]">忠誠會員 {number.format(data.summary.loyal_count)} 位；一般會員 {number.format(data.summary.member_count - data.summary.loyal_count)} 位。</p>
                      </div>
                    </div>
                  </article>
                </section>

                <article className="mt-5 rounded-3xl border border-[#19322d]/10 bg-white p-5 sm:p-7">
                  <div className="mb-6 flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
                    <div>
                      <p className="text-[10px] font-semibold tracking-[0.15em] text-[#b46346]">REVENUE COMPOSITION</p>
                      <h3 className="mt-1 text-xl font-semibold">主要品類消費金額</h3>
                    </div>
                    <p className="text-xs text-[#87938f]">僅顯示目前篩選中的前 7 名</p>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2 sm:gap-x-8">
                    {data.distributions.categories.slice(0, 7).map((item) => (
                      <div key={item.label} className="grid grid-cols-[88px_1fr] items-center gap-3">
                        <span className="truncate text-xs font-medium" title={item.label}>{item.label}</span>
                        <div>
                          <div className="mb-1 flex items-center justify-between text-[10px] text-[#7f8c88]">
                            <span>{money.format(item.revenue)}</span>
                            <span>忠誠率 {item.loyal_rate.toFixed(1)}%</span>
                          </div>
                          <div className="h-2.5 overflow-hidden rounded-full bg-[#edf0ec]">
                            <div className="h-full rounded-full bg-[#c18a62]" style={{ width: `${Math.max(3, (item.revenue / revenueMax) * 100)}%` }} />
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </article>

                <article className="mt-5 overflow-hidden rounded-3xl border border-[#19322d]/10 bg-white">
                  <div className="flex flex-col justify-between gap-4 border-b border-[#19322d]/10 p-5 sm:flex-row sm:items-end sm:p-7">
                    <div>
                      <p className="text-[10px] font-semibold tracking-[0.15em] text-[#b46346]">MEMBER EXPLORER</p>
                      <h3 className="mt-1 text-xl font-semibold">會員特徵清單</h3>
                      <p className="mt-1 text-xs text-[#7f8c88]">共 {number.format(data.pagination.total)} 位符合篩選條件</p>
                    </div>
                    <button
                      type="button"
                      onClick={() => void downloadCsv()}
                      disabled={downloading}
                      className="rounded-xl bg-[#173c35] px-4 py-2.5 text-center text-xs font-semibold text-white transition hover:bg-[#255b50] disabled:cursor-wait disabled:opacity-60"
                    >
                      {downloading ? '準備 CSV…' : '下載篩選後 CSV'}
                    </button>
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[980px] border-collapse text-left text-sm">
                      <thead className="bg-[#f8f5ef] text-[11px] uppercase tracking-[0.06em] text-[#72817c]">
                        <tr>
                          {['會員編號', '會員類型', '交易次數', '累積消費', '平均交易', '活躍年數', '年均交易', '平均間隔', '主要品類'].map((label) => <th key={label} className="whitespace-nowrap px-5 py-3 font-semibold">{label}</th>)}
                        </tr>
                      </thead>
                      <tbody>
                        {data.records.map((row) => (
                          <tr key={row.member_id} className="border-t border-[#19322d]/8 transition hover:bg-[#faf8f3]">
                            <td className="px-5 py-4 font-mono text-xs font-semibold">{row.member_id}</td>
                            <td className="px-5 py-4">
                              <span className={`rounded-full px-2.5 py-1 text-[10px] font-bold ${row.is_loyal ? 'bg-[#b46346]/12 text-[#a34e35]' : 'bg-[#19322d]/7 text-[#5d6d67]'}`}>{row.is_loyal ? '忠誠會員' : '一般會員'}</span>
                            </td>
                            <td className="px-5 py-4 font-mono text-xs">{number.format(row.total_tx)}</td>
                            <td className="px-5 py-4 font-mono text-xs">{money.format(row.total_amount)}</td>
                            <td className="px-5 py-4 font-mono text-xs">{money.format(row.avg_amount)}</td>
                            <td className="px-5 py-4">{row.active_years} 年</td>
                            <td className="px-5 py-4">{row.tx_per_year.toFixed(1)} 筆</td>
                            <td className="px-5 py-4">{row.avg_gap_days > 0 ? `${row.avg_gap_days.toFixed(1)} 天` : '—'}</td>
                            <td className="px-5 py-4 font-medium">{row.top_category}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="flex items-center justify-between gap-4 border-t border-[#19322d]/10 px-5 py-4 sm:px-7">
                    <p className="text-xs text-[#7f8c88]">第 {data.pagination.page} / {data.pagination.page_count} 頁</p>
                    <div className="flex gap-2">
                      <button type="button" disabled={data.pagination.page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))} className="rounded-lg border border-[#19322d]/15 px-3 py-2 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-35">上一頁</button>
                      <button type="button" disabled={data.pagination.page >= data.pagination.page_count} onClick={() => setPage((value) => Math.min(data.pagination.page_count, value + 1))} className="rounded-lg border border-[#19322d]/15 px-3 py-2 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-35">下一頁</button>
                    </div>
                  </div>
                </article>

                <details className="mt-5 rounded-3xl border border-[#19322d]/10 bg-[#e8e2d6] p-5 sm:p-7">
                  <summary className="cursor-pointer list-none font-semibold">KPI 定義、資料限制與 API 介接說明 ＋</summary>
                  <div className="mt-5 grid gap-6 text-sm leading-6 text-[#52645f] lg:grid-cols-3">
                    <div>
                      <h4 className="font-semibold text-[#19322d]">KPI 口徑</h4>
                      <p className="mt-2">會員數為 API 回傳的有效會員列數；平均交易金額為累積消費金額除以累積交易次數；忠誠會員占比以 is_loyal = 1 為分子。</p>
                    </div>
                    <div>
                      <h4 className="font-semibold text-[#19322d]">資料限制</h4>
                      <p className="mt-2">
                        {data.meta.mode === 'mock-api'
                          ? '目前為 720 筆抽樣測試資料，只用來驗證介面、篩選與 API 契約；數字不能當作全體會員的正式結論。'
                          : '目前資料由 Dashboard API 提供；正式顧客資料上線前，必須完成 API 身分驗證與權限檢查。'}
                      </p>
                    </div>
                    <div>
                      <h4 className="font-semibold text-[#19322d]">正式 API</h4>
                      <p className="mt-2">GCP 端回傳相同 v1 契約，並支援 category、loyalty、activeYears、frequency、search、page 與 pageSize 查詢參數。前端設定 Cloud Run 服務根網址後，會自動呼叫 /v1/dashboard。</p>
                    </div>
                  </div>
                </details>
              </>
            )}

            <footer className="flex flex-col justify-between gap-2 px-1 pb-2 pt-6 text-[11px] text-[#7f8c88] sm:flex-row">
              <p>億進寢具會員行為洞察中心 · {isGcpIapMode ? 'GCP IAP' : isRemoteApi ? 'GCP API' : '本機模擬'}</p>
              <p>{data ? `資料最後更新：${formatUpdatedAt(data.meta.generated_at)}` : '等待 API 回應'}</p>
            </footer>
          </div>
        </div>
      </div>
    </main>
  );
}
