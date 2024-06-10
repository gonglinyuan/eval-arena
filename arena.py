from typing import List, Optional
import math
from collections import Counter

import numpy as np
import numpy.random as rng
import scipy.stats as stats
import pandas as pd

def pass1_to_battle(result: pd.DataFrame, thres=0.5):
    pa = pd.merge(result, result, on=['example_id'], suffixes=["_a", "_b"], how='outer')
    pa = pa[pa['model_a'] != pa['model_b']]
    awins = (pa['pass1_a'] > thres) & (pa['pass1_b'] <= thres)
    bwins = (pa['pass1_a'] <= thres) & (pa['pass1_b'] > thres)
    ties_neither = (pa['pass1_a'] <= thres) & (pa['pass1_b'] <= thres)
    ties_both = (pa['pass1_a'] > thres) & (pa['pass1_b'] > thres)
    pa.loc[awins, 'winner'] = 'model_a'
    pa.loc[bwins, 'winner'] = 'model_b'
    pa.loc[ties_neither, 'winner'] = 'neither'
    pa.loc[ties_both, 'winner'] = 'both'
    return pa

def _comp_stats(outcomes: pd.Series):
    sufs = Counter(outcomes.values) # model_a, model_b, neither, both are the possible outcomes
    total = sufs.total()
    model_a, model_b, both, neither = sufs['model_a'], sufs['model_b'], sufs['both'], sufs['neither']
    assert model_a + model_b + both + neither == total
    pa = model_a / total
    pb = model_b / total
    diff = model_a - model_b
    sum = model_a + model_b
    std_count = np.sqrt(total * (pa*(1-pa) +  pb*(1-pb) + 2*pa*pb))
    res = dict(
        sum = sum,
        diff = diff,
        accA = (model_a + both) / total,
        accB = (model_b + both) / total,
        total = total,
        pvalue = stats.binomtest(model_a, sum, p=0.5).pvalue,
        std_count = std_count,
        std_acc = std_count / total,
    )
    return res

def battle_summary(battles):
    data_sz = len(set(battles['example_id']))
    diffvsum = battles[['model_a', 'model_b', 'winner']]\
        .groupby(['model_a', 'model_b'])\
        .aggregate(_comp_stats)\
        ['winner'].apply(pd.Series)\
        .reset_index(drop=False)
    return diffvsum
    
def compute_mle_elo(
    df, SCALE=400, BASE=10, INIT_RATING=1000, ref_model="gpt-3.5-turbo-0613",
):
    """
    calculate Elo based on winrate, code from chatbot arena (https://chat.lmsys.org/)
    with minor changes to use gpt-3.5 as as reference when possible
    """
    from sklearn.linear_model import LogisticRegression
    ptbl_a_win = pd.pivot_table(
        df[df["winner"] == "model_a"],
        index="model_a",
        columns="model_b",
        aggfunc="size",
        fill_value=0,
    )
    # if no tie, create a zero matrix
    if sum(df["winner"].isin(["both", "neither"])) == 0:
        ptbl_tie = pd.DataFrame(0, index=ptbl_a_win.index, columns=ptbl_a_win.columns)
    else:
        ptbl_tie = pd.pivot_table(
            df[df["winner"].isin(["both", "neither"])],
            index="model_a",
            columns="model_b",
            aggfunc="size",
            fill_value=0,
        )
        ptbl_tie = ptbl_tie + ptbl_tie.T

    ptbl_b_win = pd.pivot_table(
        df[df["winner"] == "model_b"],
        index="model_a",
        columns="model_b",
        aggfunc="size",
        fill_value=0,
    )
    ptbl_win = ptbl_a_win * 2 + ptbl_b_win.T * 2 + ptbl_tie

    models = pd.Series(np.arange(len(ptbl_win.index)), index=ptbl_win.index)

    p = len(models)
    X = np.zeros([p * (p - 1) * 2, p])
    Y = np.zeros(p * (p - 1) * 2)

    cur_row = 0
    sample_weights = []
    for m_a in ptbl_win.index:
        for m_b in ptbl_win.columns:
            if m_a == m_b:
                continue
            # if nan skip
            if math.isnan(ptbl_win.loc[m_a, m_b]) or math.isnan(ptbl_win.loc[m_b, m_a]):
                continue
            X[cur_row, models[m_a]] = +math.log(BASE)
            X[cur_row, models[m_b]] = -math.log(BASE)
            Y[cur_row] = 1.0
            sample_weights.append(ptbl_win.loc[m_a, m_b])

            X[cur_row + 1, models[m_a]] = math.log(BASE)
            X[cur_row + 1, models[m_b]] = -math.log(BASE)
            Y[cur_row + 1] = 0.0
            sample_weights.append(ptbl_win.loc[m_b, m_a])
            cur_row += 2
    X = X[:cur_row]
    Y = Y[:cur_row]

    lr = LogisticRegression(fit_intercept=False, penalty=None, tol=1e-6)
    lr.fit(X, Y, sample_weight=sample_weights)
    elo_scores = SCALE * lr.coef_[0] + INIT_RATING
    if ref_model in models.index:
        elo_scores += 1000 - elo_scores[models[ref_model]]
    return pd.Series(elo_scores, index=models.index).sort_values(ascending=False)

def model_table(battles, result):
    win_rates = battles[['model_a', 'model_b', 'winner']]\
        .groupby(['model_a'])\
        .aggregate({'winner': lambda x: Counter(x)['model_a'] / Counter(x).total()})\
        .reset_index().rename(columns={'winner': 'win_rate'})

    model_elos = compute_mle_elo(battles).to_frame('elo').reset_index()
    win_elo = win_rates.merge(model_elos, on='model_a')
    accs = result.groupby('model').agg(pass1=('pass1', 'mean')).reset_index()

    def sample_std(pass1s):
        N = len(pass1s)
        p = pass1s.to_numpy()
        return np.sqrt( 1 / len(p) * np.mean(p*(1-p)))

    # add std if pass1 is not just 0 or 1 
    std = result.groupby('model').agg(std=('pass1', sample_std)).reset_index()

    if any((std['std'] > 0) & (std['std'] < 1)):
        accs = accs.merge(std, on='model')[['model', 'pass1', 'std']]
        table_inds = ['model', 'pass1', 'std', 'win_rate', 'elo']
    else:
        table_inds = ['model', 'pass1', 'win_rate', 'elo']

    all_stats = win_elo.merge(accs, left_on='model_a', right_on='model')[table_inds].sort_values(by='pass1', ascending=False)
    return all_stats

def example_table(result, all_stats):
    records = []
    ids = set(result['example_id']) 
    for current_id in list(ids):
        example_data = result[result['example_id'] == current_id][['model', 'pass1']]
        example_data['correct'] = np.where(example_data['pass1'] > 0, 1, 0)
        ex = example_data[['model', 'correct']].merge(all_stats[['model', 'elo', 'pass1']], left_on = 'model', right_on = 'model')
        r = {}
        r['example_id'] = current_id
        solved_ex = ex[ex['correct'] == 1]
        r['min_elo'] = solved_ex['elo'].min()
        r['num_solved'] = len(solved_ex)
        r['models'] = solved_ex['model'].to_numpy()
        r['acc'] = len(solved_ex) / len(ex)
        r['tau'] = stats.kendalltau(ex['correct'], ex['pass1']).statistic
        # r['corr'] = stats.pearsonr(ex['correct'], ex['pass1']).statistic
        records.append(r)

    return pd.DataFrame(records)
