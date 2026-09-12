import { NextRequest, NextResponse } from 'next/server';
import members from '../../../data/member_features_test.json';

type Member = (typeof members)[number];

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const configuredDashboardApiUrl = process.env.DASHBOARD_API_URL?.trim() ?? '';
const configuredDashboardApiAudience = process.env.DASHBOARD_API_AUDIENCE?.trim() ?? '';
const configuredDashboardApiKey = process.env.DASHBOARD_API_KEY?.trim() ?? '';
const metadataIdentityEndpoint =
  'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity';

let cachedIdentityToken = '';
let cachedIdentityTokenExpiry = 0;

const frequencyOrder = ['1 筆', '2–3 筆', '4–9 筆', '10 筆以上'];

function normalizedDashboardEndpoint() {
  const normalized = configuredDashboardApiUrl.replace(/\/+$/, '');
  return normalized.endsWith('/v1/dashboard') ? normalized : `${normalized}/v1/dashboard`;
}

function dashboardApiAudience() {
  if (configuredDashboardApiAudience) return configuredDashboardApiAudience.replace(/\/+$/, '');
  return configuredDashboardApiUrl.replace(/\/v1\/dashboard\/?$/, '').replace(/\/+$/, '');
}

function identityTokenExpiry(token: string) {
  try {
    const segment = token.split('.')[1];
    if (!segment) return 0;
    const payload = JSON.parse(Buffer.from(segment, 'base64url').toString('utf8')) as { exp?: number };
    return typeof payload.exp === 'number' ? payload.exp * 1000 : 0;
  } catch {
    return 0;
  }
}

async function cloudRunIdentityToken() {
  if (cachedIdentityToken && cachedIdentityTokenExpiry > Date.now() + 60_000) {
    return cachedIdentityToken;
  }

  const audience = dashboardApiAudience();
  if (!audience) throw new Error('DASHBOARD_API_AUDIENCE is not configured.');

  const response = await fetch(`${metadataIdentityEndpoint}?audience=${encodeURIComponent(audience)}`, {
    headers: { 'Metadata-Flavor': 'Google' },
    cache: 'no-store',
  });
  if (!response.ok) throw new Error(`Metadata identity token request failed (${response.status}).`);

  const token = (await response.text()).trim();
  if (!token) throw new Error('Metadata identity token response was empty.');
  cachedIdentityToken = token;
  cachedIdentityTokenExpiry = identityTokenExpiry(token);
  return token;
}

async function proxyDashboardRequest(request: NextRequest) {
  try {
    const authorization = configuredDashboardApiKey
      ? `Bearer ${configuredDashboardApiKey}`
      : `Bearer ${await cloudRunIdentityToken()}`;
    const upstreamUrl = new URL(normalizedDashboardEndpoint());
    upstreamUrl.search = request.nextUrl.search;

    const upstream = await fetch(upstreamUrl, {
      headers: {
        Authorization: authorization,
        Accept: request.headers.get('Accept') ?? '*/*',
      },
      cache: 'no-store',
    });

    const headers = new Headers();
    for (const name of ['Content-Type', 'Content-Disposition', 'Cache-Control']) {
      const value = upstream.headers.get(name);
      if (value) headers.set(name, value);
    }
    headers.set('Cache-Control', 'no-store');
    headers.set('X-Content-Type-Options', 'nosniff');

    return new NextResponse(upstream.body, {
      status: upstream.status,
      headers,
    });
  } catch (error) {
    console.error('dashboard_proxy_failed', {
      errorType: error instanceof Error ? error.name : 'UnknownError',
    });
    return NextResponse.json(
      {
        error: {
          code: 'DASHBOARD_PROXY_UNAVAILABLE',
          message: '目前無法連線至會員分析服務，請稍後再試。',
        },
      },
      { status: 503, headers: { 'Cache-Control': 'no-store' } },
    );
  }
}

function round(value: number, digits = 2) {
  const scale = 10 ** digits;
  return Math.round(value * scale) / scale;
}

function frequencyBand(totalTx: number) {
  if (totalTx <= 1) return '1 筆';
  if (totalTx <= 3) return '2–3 筆';
  if (totalTx <= 9) return '4–9 筆';
  return '10 筆以上';
}

function csvCell(value: string | number) {
  const text = String(value ?? '');
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

export async function GET(request: NextRequest) {
  if (configuredDashboardApiUrl) {
    return proxyDashboardRequest(request);
  }

  const params = request.nextUrl.searchParams;
  const category = params.get('category') ?? 'all';
  const loyalty = params.get('loyalty') ?? 'all';
  const activeYears = params.get('activeYears') ?? 'all';
  const frequency = params.get('frequency') ?? 'all';
  const search = (params.get('search') ?? '').trim().toLowerCase();
  const pageSize = Math.min(50, Math.max(10, Number(params.get('pageSize') ?? 12)));
  const requestedPage = Math.max(1, Number(params.get('page') ?? 1));

  const sourceRows = members as Member[];
  const rows = sourceRows.filter((row) => {
    if (category !== 'all' && row.top_category !== category) return false;
    if (loyalty === 'loyal' && row.is_loyal !== 1) return false;
    if (loyalty === 'non-loyal' && row.is_loyal !== 0) return false;
    if (activeYears !== 'all' && row.active_years !== Number(activeYears)) return false;
    if (frequency !== 'all' && frequencyBand(row.total_tx) !== frequency) return false;
    if (search && !row.member_id.toLowerCase().includes(search)) return false;
    return true;
  });

  if (params.get('download') === 'csv') {
    const headers = [
      'Member ID',
      'Loyalty',
      'Total Transactions',
      'Total Amount',
      'Average Amount',
      'Active Years',
      'Transactions Per Year',
      'Average Gap Days',
      'Top Category',
      'Top Category Ratio',
    ];
    const lines = rows.map((row) => [
      row.member_id,
      row.is_loyal === 1 ? '忠誠會員' : '一般會員',
      row.total_tx,
      row.total_amount,
      row.avg_amount,
      row.active_years,
      row.tx_per_year,
      row.avg_gap_days,
      row.top_category,
      row.top_cat_ratio,
    ].map(csvCell).join(','));

    return new NextResponse(`\uFEFF${headers.join(',')}\n${lines.join('\n')}`, {
      headers: {
        'Content-Type': 'text/csv; charset=utf-8',
        'Content-Disposition': 'attachment; filename="yijin-members-filtered.csv"',
      },
    });
  }

  const categoryMap = new Map<string, { count: number; revenue: number; loyal: number }>();
  const yearMap = new Map<number, number>();
  const frequencyMap = new Map<string, number>();

  for (const row of rows) {
    const categoryStats = categoryMap.get(row.top_category) ?? { count: 0, revenue: 0, loyal: 0 };
    categoryStats.count += 1;
    categoryStats.revenue += row.total_amount;
    categoryStats.loyal += row.is_loyal;
    categoryMap.set(row.top_category, categoryStats);
    yearMap.set(row.active_years, (yearMap.get(row.active_years) ?? 0) + 1);
    const band = frequencyBand(row.total_tx);
    frequencyMap.set(band, (frequencyMap.get(band) ?? 0) + 1);
  }

  const memberCount = rows.length;
  const transactionCount = rows.reduce((sum, row) => sum + row.total_tx, 0);
  const totalRevenue = rows.reduce((sum, row) => sum + row.total_amount, 0);
  const loyalRows = rows.filter((row) => row.is_loyal === 1);
  const loyalCount = loyalRows.length;
  const validGapRows = rows.filter((row) => row.avg_gap_days > 0);
  const repeatCount = rows.filter((row) => row.total_tx > 1).length;
  const loyalRevenue = loyalRows.reduce((sum, row) => sum + row.total_amount, 0);

  const categories = [...categoryMap.entries()]
    .map(([label, value]) => ({
      label,
      count: value.count,
      share: memberCount ? round((value.count / memberCount) * 100, 1) : 0,
      revenue: round(value.revenue, 0),
      loyal_rate: value.count ? round((value.loyal / value.count) * 100, 1) : 0,
    }))
    .sort((a, b) => b.count - a.count);

  const pageCount = Math.max(1, Math.ceil(memberCount / pageSize));
  const page = Math.min(requestedPage, pageCount);
  const start = (page - 1) * pageSize;
  const records = rows.slice(start, start + pageSize).map((row) => ({
    member_id: row.member_id,
    is_loyal: row.is_loyal === 1,
    total_tx: row.total_tx,
    total_amount: row.total_amount,
    avg_amount: row.avg_amount,
    active_years: row.active_years,
    tx_per_year: row.tx_per_year,
    avg_gap_days: row.avg_gap_days,
    top_category: row.top_category,
    top_cat_ratio: row.top_cat_ratio,
  }));

  const sourceCategoryCounts = new Map<string, number>();
  for (const row of sourceRows) {
    sourceCategoryCounts.set(row.top_category, (sourceCategoryCounts.get(row.top_category) ?? 0) + 1);
  }

  return NextResponse.json({
    meta: {
      source: 'member_features_test.json',
      mode: 'mock-api',
      generated_at: '2026-07-01T00:00:00+08:00',
      version: 'mock-202607',
      source_rows: sourceRows.length,
      filtered_rows: memberCount,
      api_contract: 'v1',
    },
    applied_filters: { category, loyalty, activeYears, frequency, search },
    filter_options: {
      categories: [...sourceCategoryCounts.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([label]) => label),
      active_years: [...new Set(sourceRows.map((row) => row.active_years))].sort(),
      frequency_bands: frequencyOrder,
    },
    summary: {
      member_count: memberCount,
      transaction_count: transactionCount,
      total_revenue: round(totalRevenue, 0),
      avg_transaction_value: transactionCount ? round(totalRevenue / transactionCount, 0) : 0,
      loyal_count: loyalCount,
      loyal_rate: memberCount ? round((loyalCount / memberCount) * 100, 1) : 0,
      repeat_member_rate: memberCount ? round((repeatCount / memberCount) * 100, 1) : 0,
      avg_gap_days: validGapRows.length
        ? round(validGapRows.reduce((sum, row) => sum + row.avg_gap_days, 0) / validGapRows.length, 1)
        : null,
      avg_gap_sample: validGapRows.length,
    },
    distributions: {
      categories,
      active_years: [...yearMap.entries()]
        .sort((a, b) => a[0] - b[0])
        .map(([label, count]) => ({ label: `${label} 年`, count, share: memberCount ? round((count / memberCount) * 100, 1) : 0 })),
      frequency: frequencyOrder.map((label) => ({
        label,
        count: frequencyMap.get(label) ?? 0,
        share: memberCount ? round(((frequencyMap.get(label) ?? 0) / memberCount) * 100, 1) : 0,
      })),
      loyalty: [
        { label: '忠誠會員', count: loyalCount, share: memberCount ? round((loyalCount / memberCount) * 100, 1) : 0 },
        { label: '一般會員', count: memberCount - loyalCount, share: memberCount ? round(((memberCount - loyalCount) / memberCount) * 100, 1) : 0 },
      ],
    },
    insights: {
      top_category: categories[0] ?? null,
      loyal_revenue_share: totalRevenue ? round((loyalRevenue / totalRevenue) * 100, 1) : 0,
      repeat_member_rate: memberCount ? round((repeatCount / memberCount) * 100, 1) : 0,
      valid_gap_sample: validGapRows.length,
    },
    records,
    pagination: {
      page,
      page_size: pageSize,
      page_count: pageCount,
      total: memberCount,
    },
  });
}
