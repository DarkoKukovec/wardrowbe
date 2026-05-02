'use client';

import Image from 'next/image';
import { Droplets, CheckCircle2, Loader2, AlertCircle, WashingMachine } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import { Skeleton } from '@/components/ui/skeleton';
import { useFamilyWashing, useFamilyMemberWash } from '@/lib/hooks/use-items';
import { Item, MemberWashingItems } from '@/lib/types';
import { toast } from 'sonner';

function getInitials(name: string) {
  return name
    .split(' ')
    .map((n) => n[0])
    .join('')
    .toUpperCase()
    .slice(0, 2);
}

function WashingItemCard({
  item,
  memberId,
  onWashed,
  isPending,
}: {
  item: Item;
  memberId: string;
  onWashed: (memberId: string, itemId: string) => void;
  isPending: boolean;
}) {
  return (
    <Card className="overflow-hidden">
      <div className="relative aspect-square bg-muted">
        {item.thumbnail_url ? (
          <Image
            src={item.thumbnail_url}
            alt={item.name || item.type}
            fill
            className="object-cover"
            sizes="(max-width: 640px) 50vw, (max-width: 768px) 33vw, 25vw"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-muted-foreground text-sm">
            {item.type}
          </div>
        )}
        <div className="absolute top-2 right-2">
          <span className="inline-flex items-center gap-1 rounded-full bg-orange-500/90 px-2 py-0.5 text-xs font-medium text-white">
            <Droplets className="h-3 w-3" />
            {item.wears_since_wash}
          </span>
        </div>
      </div>
      <CardContent className="p-3">
        <p className="text-sm font-medium truncate">{item.name || item.type}</p>
        {item.brand && (
          <p className="text-xs text-muted-foreground truncate">{item.brand}</p>
        )}
        <Button
          size="sm"
          className="mt-2 w-full"
          onClick={() => onWashed(memberId, item.id)}
          disabled={isPending}
        >
          {isPending ? (
            <Loader2 className="h-3 w-3 animate-spin mr-1" />
          ) : (
            <CheckCircle2 className="h-3 w-3 mr-1" />
          )}
          Mark as Washed
        </Button>
      </CardContent>
    </Card>
  );
}

function MemberSection({
  memberData,
  onWashed,
  pendingItemId,
}: {
  memberData: MemberWashingItems;
  onWashed: (memberId: string, itemId: string) => void;
  pendingItemId: string | null;
}) {
  return (
    <section>
      <div className="flex items-center gap-3 mb-4">
        <Avatar className="h-9 w-9">
          <AvatarImage src={memberData.member_avatar_url} />
          <AvatarFallback>{getInitials(memberData.member_name)}</AvatarFallback>
        </Avatar>
        <div>
          <h2 className="text-base font-semibold">{memberData.member_name}</h2>
          <p className="text-xs text-muted-foreground">
            {memberData.items.length} item{memberData.items.length !== 1 ? 's' : ''} need washing
          </p>
        </div>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-3">
        {memberData.items.map((item) => (
          <WashingItemCard
            key={item.id}
            item={item}
            memberId={memberData.member_id}
            onWashed={onWashed}
            isPending={pendingItemId === item.id}
          />
        ))}
      </div>
    </section>
  );
}

export default function WashingPage() {
  const { data, isLoading, isError, error } = useFamilyWashing();
  const { mutate: logWash, isPending, variables: pendingVars } = useFamilyMemberWash();

  const handleWashed = (memberId: string, itemId: string) => {
    logWash(
      { memberId, itemId },
      {
        onSuccess: () => {
          toast.success('Item marked as washed');
        },
        onError: (err) => {
          toast.error(err instanceof Error ? err.message : 'Failed to mark item as washed');
        },
      }
    );
  };

  if (isLoading) {
    return (
      <div className="space-y-8">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Family Washing</h1>
          <p className="text-muted-foreground">Clothes that need washing across your family</p>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="aspect-square rounded-lg" />
          ))}
        </div>
      </div>
    );
  }

  if (isError) {
    const message = error instanceof Error ? error.message : 'Failed to load washing list';
    const isNoFamily = message.toLowerCase().includes('not in a family');

    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Family Washing</h1>
          <p className="text-muted-foreground">Clothes that need washing across your family</p>
        </div>
        <div className="flex flex-col items-center justify-center py-16 gap-3 text-center">
          <AlertCircle className="h-10 w-10 text-muted-foreground" />
          <p className="text-muted-foreground">
            {isNoFamily
              ? 'You are not part of a family yet. Join or create a family to see the washing list.'
              : message}
          </p>
        </div>
      </div>
    );
  }

  const members = data?.members ?? [];
  const total = data?.total ?? 0;

  return (
    <div className="space-y-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Family Washing</h1>
          <p className="text-muted-foreground">Clothes that need washing across your family</p>
        </div>
        {total > 0 && (
          <span className="inline-flex items-center gap-1.5 rounded-full bg-orange-500/10 px-3 py-1 text-sm font-medium text-orange-600 border border-orange-500/20">
            <Droplets className="h-4 w-4" />
            {total} item{total !== 1 ? 's' : ''}
          </span>
        )}
      </div>

      {members.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 gap-4 text-center">
          <WashingMachine className="h-12 w-12 text-muted-foreground" />
          <div>
            <p className="font-medium">All clean!</p>
            <p className="text-sm text-muted-foreground mt-1">
              No clothes need washing right now.
            </p>
          </div>
        </div>
      ) : (
        <div className="space-y-10">
          {members.map((memberData) => (
            <MemberSection
              key={memberData.member_id}
              memberData={memberData}
              onWashed={handleWashed}
              pendingItemId={
                isPending && pendingVars?.memberId === memberData.member_id
                  ? (pendingVars?.itemId ?? null)
                  : null
              }
            />
          ))}
        </div>
      )}
    </div>
  );
}
