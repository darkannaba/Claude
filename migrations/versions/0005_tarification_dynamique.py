"""tarification dynamique (saisons, prix par chambre, remises long séjour, historique des prix)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-05 20:30:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Prix spécifique par chambre (TTC / nuit) : NULL = prix du type de chambre.
    with op.batch_alter_table('rooms', schema=None) as batch_op:
        batch_op.add_column(sa.Column('price_override', sa.Integer(), nullable=True))

    op.create_table('seasons',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('level', sa.String(length=10), nullable=False),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('end_date', sa.Date(), nullable=False),
    sa.Column('adjustment_percent', sa.Integer(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('room_type_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['room_type_id'], ['room_types.id'], name=op.f('fk_seasons_room_type_id_room_types'),
                            ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_seasons'))
    )
    with op.batch_alter_table('seasons', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_seasons_start_date'), ['start_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_seasons_end_date'), ['end_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_seasons_room_type_id'), ['room_type_id'], unique=False)

    op.create_table('long_stay_discounts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('min_nights', sa.Integer(), nullable=False),
    sa.Column('discount_percent', sa.Integer(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_long_stay_discounts')),
    sa.UniqueConstraint('min_nights', name=op.f('uq_long_stay_discounts_min_nights'))
    )

    op.create_table('price_changes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('target_type', sa.String(length=20), nullable=False),
    sa.Column('target_id', sa.String(length=40), nullable=False),
    sa.Column('target_label', sa.String(length=160), nullable=False),
    sa.Column('field', sa.String(length=40), nullable=False),
    sa.Column('old_value', sa.String(length=80), nullable=False),
    sa.Column('new_value', sa.String(length=80), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_price_changes_user_id_users'),
                            ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_price_changes'))
    )
    with op.batch_alter_table('price_changes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_price_changes_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_price_changes_target_type'), ['target_type'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('price_changes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_price_changes_target_type'))
        batch_op.drop_index(batch_op.f('ix_price_changes_created_at'))
    op.drop_table('price_changes')

    op.drop_table('long_stay_discounts')

    with op.batch_alter_table('seasons', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_seasons_room_type_id'))
        batch_op.drop_index(batch_op.f('ix_seasons_end_date'))
        batch_op.drop_index(batch_op.f('ix_seasons_start_date'))
    op.drop_table('seasons')

    with op.batch_alter_table('rooms', schema=None) as batch_op:
        batch_op.drop_column('price_override')
