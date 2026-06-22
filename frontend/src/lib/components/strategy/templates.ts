// Curated, editable starting points for the Strategy Creator. Each spec is a
// valid rule_engine spec (references real indicator outputs + operators), so it
// loads, previews and backtests immediately — users then tweak from there.

export type Operand = string | number | { param: string } | { const: number };

export interface Condition {
	left: Operand;
	op: string;
	right: Operand;
}

export interface Group {
	logic: 'and' | 'or';
	conditions: Array<Condition | Group>;
}

export interface RuleSpec {
	indicators: Array<{ id: string; kind: string; params: Record<string, number> }>;
	params: Record<string, number>;
	entry_long: Group | null;
	exit_long: Group | null;
	entry_short: Group | null;
	exit_short: Group | null;
}

export interface StrategyTemplate {
	id: string;
	name: string;
	category: string;
	description: string;
	symbol: string;
	timeframe: string;
	trade_mode: 'long_only' | 'short_only' | 'both';
	spec: RuleSpec;
}

export const STRATEGY_TEMPLATES: StrategyTemplate[] = [
	{
		id: 'rsi-mean-reversion',
		name: 'RSI Mean Reversion',
		category: 'Mean Reversion',
		description: 'Buy oversold dips, exit as momentum recovers. Classic contrarian entry.',
		symbol: 'BTC/USDT',
		timeframe: '1h',
		trade_mode: 'long_only',
		spec: {
			indicators: [{ id: 'rsi', kind: 'rsi', params: { length: 14 } }],
			params: { oversold: 30, exit_level: 55 },
			entry_long: { logic: 'and', conditions: [{ left: 'rsi', op: '<', right: { param: 'oversold' } }] },
			exit_long: { logic: 'or', conditions: [{ left: 'rsi', op: '>', right: { param: 'exit_level' } }] },
			entry_short: null,
			exit_short: null,
		},
	},
	{
		id: 'ema-trend-cross',
		name: 'EMA Trend Cross',
		category: 'Trend',
		description: 'Go long when a fast EMA crosses above a slow EMA; flat on the reverse cross.',
		symbol: 'ETH/USDT',
		timeframe: '4h',
		trade_mode: 'long_only',
		spec: {
			indicators: [
				{ id: 'ema_fast', kind: 'ema', params: { length: 20 } },
				{ id: 'ema_slow', kind: 'ema', params: { length: 50 } },
			],
			params: {},
			entry_long: { logic: 'and', conditions: [{ left: 'ema_fast', op: 'crosses_above', right: 'ema_slow' }] },
			exit_long: { logic: 'or', conditions: [{ left: 'ema_fast', op: 'crosses_below', right: 'ema_slow' }] },
			entry_short: null,
			exit_short: null,
		},
	},
	{
		id: 'macd-momentum',
		name: 'MACD Momentum',
		category: 'Momentum',
		description: 'Enter on a bullish MACD signal-line cross above zero; exit on the bearish cross.',
		symbol: 'BTC/USDT',
		timeframe: '1h',
		trade_mode: 'long_only',
		spec: {
			indicators: [{ id: 'macd', kind: 'macd', params: { fast: 12, slow: 26, signal: 9 } }],
			params: {},
			entry_long: {
				logic: 'and',
				conditions: [
					{ left: 'macd', op: 'crosses_above', right: 'macd_signal' },
					{ left: 'macd', op: '>', right: 0 },
				],
			},
			exit_long: { logic: 'or', conditions: [{ left: 'macd', op: 'crosses_below', right: 'macd_signal' }] },
			entry_short: null,
			exit_short: null,
		},
	},
	{
		id: 'bollinger-breakout',
		name: 'Bollinger Breakout',
		category: 'Breakout',
		description: 'Buy a close that breaks above the upper band; exit back at the mid band.',
		symbol: 'BTC/USDT',
		timeframe: '1h',
		trade_mode: 'long_only',
		spec: {
			indicators: [{ id: 'bb', kind: 'bollinger', params: { length: 20, num_std: 2 } }],
			params: {},
			entry_long: { logic: 'and', conditions: [{ left: 'close', op: 'crosses_above', right: 'bb_upper' }] },
			exit_long: { logic: 'or', conditions: [{ left: 'close', op: 'crosses_below', right: 'bb_mid' }] },
			entry_short: null,
			exit_short: null,
		},
	},
	{
		id: 'supertrend-follower',
		name: 'Supertrend Follower',
		category: 'Trend',
		description: 'Ride the trend: long while price holds above the Supertrend line.',
		symbol: 'ETH/USDT',
		timeframe: '1h',
		trade_mode: 'long_only',
		spec: {
			indicators: [{ id: 'st', kind: 'supertrend', params: { length: 10, mult: 3 } }],
			params: {},
			entry_long: { logic: 'and', conditions: [{ left: 'close', op: 'crosses_above', right: 'st' }] },
			exit_long: { logic: 'or', conditions: [{ left: 'close', op: 'crosses_below', right: 'st' }] },
			entry_short: null,
			exit_short: null,
		},
	},
	{
		id: 'stochastic-reversal',
		name: 'Stochastic Reversal',
		category: 'Mean Reversion',
		description: 'Catch reversals: %K crossing up out of oversold; exit when overbought.',
		symbol: 'BTC/USDT',
		timeframe: '15m',
		trade_mode: 'long_only',
		spec: {
			indicators: [{ id: 'stoch', kind: 'stochastic', params: { k: 14, d: 3, smooth: 3 } }],
			params: { oversold: 20, overbought: 80 },
			entry_long: {
				logic: 'and',
				conditions: [
					{ left: 'stoch_k', op: '<', right: { param: 'oversold' } },
					{ left: 'stoch_k', op: 'crosses_above', right: 'stoch_d' },
				],
			},
			exit_long: { logic: 'or', conditions: [{ left: 'stoch_k', op: '>', right: { param: 'overbought' } }] },
			entry_short: null,
			exit_short: null,
		},
	},
	{
		id: 'trend-filtered-dip',
		name: 'Trend-Filtered Dip Buy',
		category: 'Trend + Reversion',
		description: 'Only buy dips when price is above the 200 EMA (uptrend filter).',
		symbol: 'BTC/USDT',
		timeframe: '1h',
		trade_mode: 'long_only',
		spec: {
			indicators: [
				{ id: 'rsi', kind: 'rsi', params: { length: 14 } },
				{ id: 'ema200', kind: 'ema', params: { length: 200 } },
			],
			params: { dip: 40, take: 65 },
			entry_long: {
				logic: 'and',
				conditions: [
					{ left: 'close', op: '>', right: 'ema200' },
					{ left: 'rsi', op: '<', right: { param: 'dip' } },
				],
			},
			exit_long: { logic: 'or', conditions: [{ left: 'rsi', op: '>', right: { param: 'take' } }] },
			entry_short: null,
			exit_short: null,
		},
	},
	{
		id: 'funding-fade',
		name: 'Funding-Rate Fade',
		category: 'Crypto',
		description: 'Fade crowded shorts: go long when funding z-score is deeply negative.',
		symbol: 'BTC/USDT',
		timeframe: '1h',
		trade_mode: 'long_only',
		spec: {
			indicators: [{ id: 'fz', kind: 'funding_zscore', params: { length: 96 } }],
			params: { extreme: -1.5 },
			entry_long: { logic: 'and', conditions: [{ left: 'fz', op: '<', right: { param: 'extreme' } }] },
			exit_long: { logic: 'or', conditions: [{ left: 'fz', op: '>', right: 0 }] },
			entry_short: null,
			exit_short: null,
		},
	},
];
