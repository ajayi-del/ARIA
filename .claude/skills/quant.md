---
name: quant
description: Apply quantitative, probabilistic, and risk-adjusted thinking to any change
allowed-tools: [Bash, Read, Write, Edit, Agent]
when_to_use: When analyzing trading logic, risk parameters, position sizing, signal quality, or any numerically-sensitive change
arguments:
  - name: domain
    type: string
    description: "Area of analysis — sizing, signals, risk, execution, backtest"
---

# Quant Skill

Apply this lens to every proposed change. Numbers first. Intuition second.

## 1. Probabilistic Framing
- What is the base rate of this scenario?
- What is the expected value (EV) of this change?
- What is the confidence interval, not the point estimate?
- Would this survive a Monte Carlo simulation?

## 2. Risk-Adjusted Metrics
- Sharpe or Calmar impact: does this improve risk-adjusted return?
- Drawdown contribution: what is the worst-case add to max DD?
- Tail risk: what happens at 3+ sigma?
- Correlation effect: does this add new beta or pure alpha?

## 3. Statistical Rigor
- Sample size: is the evidence statistically significant?
- Look-ahead bias: are we using future information?
- Survivorship bias: are we only seeing winners?
- Overfitting risk: how many parameters vs observations?

## 4. Position Sizing Math
- Verify: `size = conviction * kelly_fraction * risk_budget / atr`
- Never change leverage without recomputing liquidation distance
- Check notional: `size * price >= min_notional` before submission
- Confirm drawdown is stored as PERCENT (8.0 = 8%), never decimal

## 5. Execution Realism
- Slippage assumption: is it conservative for the market?
- Fill probability: limit orders vs market orders
- Latency: can the signal survive the round-trip?
- Fee impact: `fee = taker_rate * notional` — is it modeled?

## Output Format
Report the numerical analysis in 3 bullets or fewer. If numbers are missing, say so explicitly.
