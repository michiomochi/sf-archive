# 目的 {color="yellow_bg"}
このエピックで追う目的を、測れる形で書きます。数値は基準値と目標値を分けて書きます。

<table fit-page-width="true" header-row="true">
<colgroup><col width="200" /><col width="700" /></colgroup>
<tr><td>項目</td><td>内容</td></tr>
<tr><td>指標</td><td>何を測るか</td></tr>
<tr><td>定義</td><td>指標の数え方と対象期間</td></tr>
<tr><td>基準値</td><td>いまの値と、測った日</td></tr>
<tr><td>目標値</td><td>どこまで動かすか</td></tr>
<tr><td>期限</td><td>いつまでに</td></tr>
<tr><td>完了条件</td><td>これが満たされたら完了とする条件</td></tr>
<tr><td>種別</td><td>プロダクト開発 または 調査。プロダクト開発だけが工程の行とカードの自動作成に入る</td></tr>
</table>

# 候補 {color="yellow_bg"}
プランナーがここからタスクを作ります。1回に最大2件で、価値ゲートを通ったものだけです。1項目に、打ち手の名前、判断の宣言、感度の見積もり、開始できるフェーズを書きます。

- [ ] 
- [ ] 

# 計測ログ {color="yellow_bg"}
指標の実測値を、測った日と出典を添えて1行ずつ残します。目標に届いたかの判断はこの記録で行います。

<table fit-page-width="true" header-row="true">
<colgroup><col width="160" /><col width="220" /><col width="200" /><col width="320" /></colgroup>
<tr><td>日付</td><td>指標</td><td>値</td><td>出典</td></tr>
<tr><td></td><td></td><td></td><td></td></tr>
</table>

# 管理ログ {color="yellow_bg"}
状態を変えた記録を1行ずつ残します。人の承認は、レビュー DB で決めた日と結果を1行で残します。逸脱や予算超過があったときも1行ずつ残し、同じ原因で2回失敗したら止めて報告します。

# 関連ドキュメント {color="yellow_bg"}
成果物と、このエピックから出たブランチへのリンクを置きます。

# 完了レポート {color="yellow_bg"}
目的の指標が目標に届いたかを、実測で書きます。完了は、リリース承認が承認されたときに決まります。

## 実施内容 {color="blue_bg"}
打ち手（候補の項目）ごとに、何をしたかを書きます。

## 結果 {color="blue_bg"}
指標の実測値と、測った日を書きます。

## 確認した方法 {color="blue_bg"}
測り方と、実行したコマンドを書きます。

## 未確認・残件 {color="blue_bg"}
確認できていないことを書きます。

## 次のタスク {color="blue_bg"}
続きがあるなら、次の行にすることが分かる形で書きます。
