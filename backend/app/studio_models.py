from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class StudioModel(BaseModel):
    model_config = ConfigDict(extra='forbid')

class PostingConfig(StudioModel):
    source: Literal['manual','pexels','ai'] = 'manual'
    asset_ids: list[str] = Field(default_factory=list, max_length=10)
    query: str = Field(default='', max_length=500)
    media_type: Literal['photo','video'] = 'photo'
    image_count: int = Field(default=3, ge=1, le=10)
    caption_model: str = Field(default='', max_length=100, pattern=r'^[A-Za-z0-9._:/-]*$')
    image_model: str = Field(default='', max_length=100, pattern=r'^[A-Za-z0-9._:/-]*$')
    caption_instructions: str = Field(default='', max_length=1000)
    caption: str = Field(default='', max_length=2200)
    hashtags: str = Field(default='', max_length=600)
    location: str = Field(default='', max_length=120)
    auto_caption: bool = False
    language: str = Field(default='中文', max_length=30)
    concurrency: int = Field(default=1, ge=1)
    interval_seconds: int = Field(default=60, ge=0, le=86400)
    scheduled_at: str = ''
    @model_validator(mode='after')
    def check_caption(self):
        self.asset_ids = list(dict.fromkeys(self.asset_ids))
        self.query = self.query.strip()
        self.location = self.location.strip()
        if self.source == 'ai' and self.media_type == 'video':
            raise ValueError('AI 生成暂支持图片；视频请选择手动加入或 Pexels')
        if len(self.caption + '\n' + self.hashtags) > 2200:
            raise ValueError('文案与标签合计不能超过 2200 字符')
        if self.media_type == 'video' and len(self.asset_ids) > 1:
            raise ValueError('视频每次选择一个素材')
        return self

class NurtureConfig(StudioModel):
    stage: Literal['适应期','稳定期','维护期'] = '适应期'
    surfaces: list[Literal['feed','reels','stories','post','profile','search']] = Field(default_factory=lambda:['reels'], min_length=1)
    targets: list[str] = Field(default_factory=list, max_length=1000)
    keywords: str = Field(default='', max_length=300)
    rounds: int = Field(default=1, ge=1, le=20)
    minutes: int = Field(default=5, ge=1, le=120, strict=True)
    dwell_min: int = Field(default=8, ge=3, le=300)
    dwell_max: int = Field(default=20, ge=3, le=300)
    # Zero means all windows selected for this plan; resolved when jobs are created.
    concurrency: int = Field(default=0, ge=0, le=1000, strict=True)
    interval_seconds: int = Field(default=0, ge=0, le=86400)
    scheduled_at: str = ''
    like_probability: int = Field(default=70, ge=0, le=100)
    like_limit: int = Field(default=0, ge=0, le=100)
    save_probability: int = Field(default=0, ge=0, le=100)
    save_limit: int = Field(default=0, ge=0, le=100)
    follow_probability: int = Field(default=0, ge=0, le=100)
    follow_limit: int = Field(default=0, ge=0, le=100)
    comment_probability: int = Field(default=0, ge=0, le=100)
    comment_limit: int = Field(default=0, ge=0, le=50)
    comments: list[str] = Field(default_factory=list, max_length=50)
    @model_validator(mode='before')
    @classmethod
    def fixed_standalone_policy(cls, value):
        # Legacy templates remain importable, but may never opt back into
        # comments, follows, another surface, scheduling or a different rate.
        if not isinstance(value, dict):
            return value
        value = dict(value)
        value.update(stage='适应期', surfaces=['reels'], targets=[], keywords='',
                     rounds=1, dwell_min=8, dwell_max=20, interval_seconds=0,
                     scheduled_at='', like_probability=70, like_limit=0,
                     save_probability=0, save_limit=0, follow_probability=0,
                     follow_limit=0, comment_probability=0, comment_limit=0,
                     comments=[])
        return value
