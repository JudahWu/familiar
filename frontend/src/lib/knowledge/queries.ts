import { useEffect } from "react"

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { knowledgeApi } from "./api"

/**
 * 知识库的 react-query 封装。
 *
 * 所有写操作成功后统一 invalidate 列表——列表里既有文档状态也有段落数，
 * 这两样都会因为一次上传而变，分开维护缓存只会让它们不一致。
 */

export const knowledgeKeys = {
  bases: ["knowledge-bases"] as const,
  progress: (docId: string | null) => ["knowledge-progress", docId] as const,
}

export function useKnowledgeBases() {
  return useQuery({
    queryKey: knowledgeKeys.bases,
    queryFn: () => knowledgeApi.listBases(),
  })
}

function useInvalidateBases() {
  const queryClient = useQueryClient()
  return () => queryClient.invalidateQueries({ queryKey: knowledgeKeys.bases })
}

export function useCreateBase() {
  const invalidate = useInvalidateBases()
  return useMutation({
    mutationFn: (name: string) => knowledgeApi.createBase(name),
    onSuccess: () => void invalidate(),
  })
}

export function useDeleteBase() {
  const invalidate = useInvalidateBases()
  return useMutation({
    mutationFn: (kbId: string) => knowledgeApi.deleteBase(kbId),
    onSuccess: () => void invalidate(),
  })
}

export function useUploadDocument() {
  const invalidate = useInvalidateBases()
  return useMutation({
    mutationFn: ({ kbId, file }: { kbId: string; file: File }) =>
      knowledgeApi.uploadDocument(kbId, file),
    onSuccess: () => void invalidate(),
  })
}

export function useDeleteDocument() {
  const invalidate = useInvalidateBases()
  return useMutation({
    mutationFn: ({ kbId, docId }: { kbId: string; docId: string }) =>
      knowledgeApi.deleteDocument(kbId, docId),
    onSuccess: () => void invalidate(),
  })
}

/**
 * 轮询一篇文档的导入进度。
 *
 * 导入是后台任务（一本书要跑几十秒），上传接口立刻返回，前端靠轮询跟进。
 * 任务结束（done / failed）就停止轮询，并刷新一次列表让状态和段落数更新。
 */
export function useImportProgress(docId: string | null) {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: knowledgeKeys.progress(docId),
    queryFn: () => knowledgeApi.getProgress(docId as string),
    enabled: Boolean(docId),
    refetchInterval: (current) => {
      const status = current.state.data?.status
      return status === "done" || status === "failed" ? false : 1000
    },
  })

  const status = query.data?.status
  useEffect(() => {
    if (status === "done" || status === "failed") {
      void queryClient.invalidateQueries({ queryKey: knowledgeKeys.bases })
    }
  }, [status, queryClient])

  return query
}
