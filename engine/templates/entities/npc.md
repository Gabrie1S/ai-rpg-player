---
id: npc_xxx
name: 某角色
tier: B                  # A 核心（每次推演）/ B 重要（出场更新）/ C 路人
alive: true
locked: false
faction: null            # 势力 ID
location: null           # 地点 ID
brief: ""                # 一句话明面身份，会出现在索引里
met_player: null         # 主角第一次见到他的游戏日；没见过为 null
last_seen: null          # 主角上次见到他的游戏日

# 角色实力与特征（字段名由当前题材包 rules.yaml 的 schema_mapping 定义）
rank: 平民
affinity: {}
aptitude: {体能: 中, 直觉: 中}
core_engine: null
attributes: {体能: 2, 反应: 2, 智力: 2, 魅力: 2}
skills: []
gear: []
abilities: []

# 对玩家的态度（-100 ~ 100）
toward_player: {好感: 0, 信任: 0, 敬畏: 0}
thresholds: []           # - {when: 信任 >= 50, unlock: 说出秘密}
traits_effect: []        # - 记仇：负面事件好感变化 ×2

# 动态状态
goal: ""
activity: ""
mood: ""

# 个人剧情线（A 级必填，B 级可选）
questline: null
---

## 外貌

## 性格

矛盾：

## 想要 / 害怕 / 底线

- 想要：
- 害怕：
- 底线：

## 秘密

- 内容：
  引爆条件：

## 说话风格与台词示例

风格：

- "……"
- "……"

## 认知

<!-- - [第N天][亲眼/听说/推测][真/假/半真] 内容 -->

## 重要时刻（永不压缩）

<!-- - 日期（第 N 天）：发生了什么，关键细节〔原文：log/story/…〕 -->

## 与玩家的记忆

<!-- - 第N天：摘要 -->

## 关系

<!-- - npc_xxx：搭档，利益捆绑但互相提防 -->
